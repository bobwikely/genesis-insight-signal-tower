"""
Signal Tower - 生产级候选人排序系统
第一阶段实现：安全性、数据持久化、错误处理
"""

import streamlit as st
import json
import pandas as pd
import requests
import sqlite3
import hashlib # For file hashing
import re # For filename sanitization
import os
import logging
import structlog # For structured logging
import threading # For thread-safe singleton
import time
from datetime import datetime
from typing import List, Dict, Any, Optional, Union, Tuple
from contextlib import contextmanager # For database connection context
from functools import lru_cache # For caching function results
from tenacity import retry, stop_after_attempt, wait_exponential # For API retries
from dataclasses import dataclass
from pydantic import BaseModel, Field, validator, ValidationError, root_validator # For enhanced data validation

# ================== 配置管理 ==================
@dataclass
class Config:
    """集中化配置管理"""
    FASTAPI_BACKEND_URL: str
    MAX_FILE_SIZE: int = 5 * 1024 * 1024  # 5MB
    MAX_FILES: int = 50
    DB_PATH: str = "data/candidates.db" # Database path within the app directory
    LOG_LEVEL: str = "INFO"
    SESSION_TIMEOUT: int = 3600  # 1小时 (for Streamlit cache)
    
    @classmethod
    def from_env(cls):
        """Loads configuration from environment variables, with defaults."""
        return cls(
            FASTAPI_BACKEND_URL=os.getenv(
                "FASTAPI_BACKAPI_URL", # Changed to FASTAPI_BACKAPI_URL for consistency with common env var naming
                "https://genesis-insight-fastapi-backend.onrender.com"
            ),
            DB_PATH=os.getenv("DB_PATH", "data/candidates.db"),
            LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO")
        )

# Load configuration
config = Config.from_env()

# ================== 结构化日志配置 ==================
def setup_logging():
    """配置结构化日志系统"""
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            # Use JSONRenderer for production, ConsoleRenderer for development
            structlog.processors.JSONRenderer() if os.getenv("ENVIRONMENT") == "production" 
            else structlog.dev.ConsoleRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    # Configure standard logging to use structlog's logger
    logging.basicConfig(level=log_level)

# Setup logging once at the start
setup_logging()
logger = structlog.get_logger(__name__)

# ================== 安全增强模块 ==================
class SecurityManager:
    """安全管理器 - 处理所有安全相关功能"""
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """清理文件名，防止路径遍历攻击"""
        # 移除危险字符，只保留字母数字、点、横线、下划线
        safe_name = re.sub(r'[^\w\s-.]', '', filename.strip())
        # 限制长度
        safe_name = safe_name[:100]
        # 防止隐藏文件和特殊文件名
        if safe_name.startswith('.') or safe_name.lower() in ['con', 'prn', 'aux', 'nul']:
            safe_name = f"file_{safe_name}"
        
        return safe_name or "unnamed_file"
    
    @staticmethod
    def validate_file_content(content: bytes) -> Tuple[bool, str]:
        """验证文件内容安全性"""
        try:
            # 检查文件大小
            if len(content) > config.MAX_FILE_SIZE:
                return False, f"文件大小超限 ({len(content)} bytes > {config.MAX_FILE_SIZE} bytes)"
            
            # 检查是否为空文件
            if len(content.strip()) == 0:
                return False, "文件内容为空"
            
            # 尝试解码为 UTF-8
            try:
                content_str = content.decode('utf-8')
            except UnicodeDecodeError:
                return False, "文件编码格式不支持，请使用 UTF-8"
            
            # 检查是否为有效 JSON 结构（只检查前 2000 字符）
            preview = content_str[:2000].strip()
            if not (preview.startswith('{') and '}' in preview):
                return False, "文件格式不正确，需要 JSON 格式 (未检测到有效的JSON对象开始/结束)"
            
            # 基础 JSON 语法检查
            try:
                json.loads(preview) # Try loading just the preview for quick syntax check
            except json.JSONDecodeError as e:
                return False, f"JSON 格式错误: {str(e)[:100]}"
            
            return True, "文件内容验证通过"
            
        except Exception as e:
            logger.error("文件内容验证异常", error=str(e))
            return False, f"文件验证失败: {str(e)[:100]}"
    
    @staticmethod
    def generate_file_hash(content: str) -> str:
        """生成文件内容哈希值，用于去重和缓存"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]

security = SecurityManager()

# ================== 错误处理系统 ==================
class ErrorHandler:
    @staticmethod
    def handle_file_error(e: Exception, filename: str) -> str:
        """Generates a unique error ID and logs file processing errors."""
        error_id = f"ERR_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(str(e)) % 10000:04d}"
        logger.error("File processing error", 
                    error_id=error_id,
                    filename=filename,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    stack_trace=str(e.__traceback__))
        return f"文件处理失败 (错误ID: {error_id} - {type(e).__name__}: {str(e)})"

# ================== 数据持久化系统 ==================
class DataStore:
    """线程安全的数据存储管理器 (SQLite Singleton)"""
    
    _instance = None
    _lock = threading.Lock() # Thread-safe singleton initialization
    
    def __new__(cls):
        """Implements the singleton pattern for DataPersistence."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False # Flag to ensure init_db is called once
        return cls._instance
    
    def __init__(self):
        """Initializes the DataStore instance."""
        if not self._initialized:
            self.db_path = config.DB_PATH
            self.init_db()
            self._initialized = True
            logger.info("数据存储系统初始化完成", db_path=self.db_path)
    
    def init_db(self):
        """初始化数据库和表结构"""
        # 确保数据目录存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        with self.get_connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_hash TEXT UNIQUE, -- Unique hash for content deduplication
                    data TEXT NOT NULL, -- Store raw JSON as text
                    status TEXT DEFAULT '所有状态',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS system_info (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                -- 创建索引提高查询性能
                CREATE INDEX IF NOT EXISTS idx_candidates_name ON candidates(name);
                CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
                CREATE INDEX IF NOT EXISTS idx_candidates_hash ON candidates(file_hash);
                
                -- 插入系统信息 (如果不存在)
                INSERT OR IGNORE INTO system_info (key, value) 
                VALUES ('db_version', '1.0'), ('created_at', datetime('now'));
            ''')
        logger.info("数据库初始化完成", db_path=self.db_path)
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path, 
                timeout=10.0,
                check_same_thread=False # Allow multi-thread access for Streamlit
            )
            conn.row_factory = sqlite3.Row # Return dictionary-like rows
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        except Exception as e:
            logger.error("数据库连接错误", error=str(e), db_path=self.db_path)
            if conn:
                conn.rollback() # Rollback on error
            raise # Re-raise the exception
        finally:
            if conn:
                conn.close()
    
    def save_candidate(self, name: str, data: dict, file_hash: str) -> bool:
        """保存或更新候选人数据。如果 file_hash 存在则更新，否则插入。"""
        try:
            with self.get_connection() as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO candidates 
                    (name, file_hash, data, updated_at) 
                    VALUES (?, ?, ?, datetime('now'))
                ''', (name, file_hash, json.dumps(data, ensure_ascii=False)))
                conn.commit()
                
            logger.info("候选人数据保存成功", name=name, file_hash=file_hash)
            return True
            
        except Exception as e:
            logger.error("保存候选人数据失败", name=name, file_hash=file_hash, error=str(e))
            return False
    
    def load_all_candidates(self) -> List[Dict]:
        """加载所有候选人数据，并附带数据库状态和时间戳信息"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute('''
                    SELECT name, data, status, created_at, updated_at 
                    FROM candidates 
                    ORDER BY updated_at DESC
                ''')
                
                candidates = []
                for row in cursor:
                    try:
                        data = json.loads(row['data'])
                        # Add database-specific metadata to the loaded data
                        data['_db_status'] = row['status']
                        data['_created_at'] = row['created_at']
                        data['_updated_at'] = row['updated_at']
                        candidates.append(data)
                    except json.JSONDecodeError as e:
                        logger.error("候选人数据解析失败 (DB)", name=row['name'], error=str(e))
                        continue
                
                logger.info("候选人数据加载完成", count=len(candidates))
                return candidates
                
        except Exception as e:
            logger.error("加载候选人数据失败 (DB)", error=str(e))
            return []
    
    def update_candidate_status(self, name: str, status: str) -> bool:
        """更新候选人状态"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute('''
                    UPDATE candidates 
                    SET status = ?, updated_at = datetime('now')
                    WHERE name = ?
                ''', (status, name))
                
                if cursor.rowcount == 0:
                    logger.warning("更新候选人状态失败，候选人不存在", name=name)
                    return False
                
                conn.commit()
                logger.info("候选人状态更新成功", name=name, status=status)
                return True
                
        except Exception as e:
            logger.error("更新候选人状态失败", name=name, error=str(e))
            return False
    
    def clear_all_data(self):
        """清空所有候选人数据表"""
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM candidates")
                conn.commit()
            logger.info("所有候选人数据已清空 (DB)。")
        except Exception as e:
            logger.error("清空所有数据失败 (DB)", error=str(e))
            raise

# Initialize DataStore (singleton ensures only one instance)
db_manager = DataStore()

# --- API Client for Backend Communication (with Retries) ---
class APIClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session() # Use a session for connection pooling
        logger.info(f"APIClient initialized with base URL: {base_url}")
        
    def _get_headers(self):
        """Returns headers for API requests (placeholder for auth)."""
        headers = {"Content-Type": "application/json"}
        # if self.api_key: # Future: Add API key authentication
        #     headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def test_connection(self, endpoint: str = "/"):
        """Tests connection to a given endpoint with retries."""
        try:
            response = self.session.get(f"{self.base_url}{endpoint}", timeout=10)
            response.raise_for_status()
            logger.info("API connection test successful", endpoint=endpoint)
            return response.status_code, response.json()
        except requests.exceptions.RequestException as e:
            logger.error("API connection test failed", endpoint=endpoint, error=str(e))
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def sort_candidates(self, data: dict):
        """Calls the backend API to sort candidates with retry logic."""
        try:
            response = self.session.post(
                f"{self.base_url}/sort-candidates/",
                json=data,
                headers=self._get_headers(),
                timeout=120 # Long timeout for potentially heavy AI processing
            )
            response.raise_for_status()
            logger.info("Sort candidates API call successful")
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error("Sort candidates API call failed", error=str(e))
            raise

api_client = APIClient(config.FASTAPI_BACKEND_URL)

# --- Pydantic Models for Robust Data Validation ---
# These models ensure that incoming JSON data conforms to expected structures.
class PreliminarySkillsMatchDetail(BaseModel):
    skill_point: str
    match_level: str # e.g., "高", "中", "低"
    evidence: str

class PreliminarySkillsMatch(BaseModel):
    overall_assessment: str # e.g., "高", "中", "低"
    details: List[PreliminarySkillsMatchDetail]

class FullStatePotentialAnalysisDetail(BaseModel):
    potential_point: str
    potential_level: str # e.g., "高", "中", "低"
    evidence: str

class FullStatePotentialAnalysis(BaseModel):
    overall_assessment: str # e.g., "具备潜力", "部分潜力", "暂无明显潜力"
    details: List[FullStatePotentialAnalysisDetail]

class IntendedRoleAnalysis(BaseModel):
    role_name: str
    overall_match_score: float = Field(..., ge=0, le=100)
    score_reason: str
    preliminary_skills_match: PreliminarySkillsMatch
    full_state_potential_analysis: FullStatePotentialAnalysis

class HumanAICollaborationFit(BaseModel):
    assessment: Union[str, float] # Can be "高", "中", "低" or a number
    reason: str = "根据匹配度推断" # Provide a default reason if not given

    @validator('assessment', pre=True, always=True)
    def convert_assessment(cls, v):
        """Validates and converts human_ai_collaboration_fit assessment."""
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str) and v.lower() in ['未知', 'unknown', '高', '中', '低']:
            return v
        try:
            score = float(v)
            if 0 <= score <= 100:
                return score
            else:
                raise ValueError("Score must be between 0 and 100 or a valid string.")
        except (ValueError, TypeError):
            logger.warning(f"Invalid human_ai_collaboration_fit assessment value: {v}. Defaulting to '未知'.")
            return "未知"

class CandidateReportModel(BaseModel):
    applicant_name: str = Field(..., min_length=1, max_length=100)
    analysis_date: str
    
    # Fields that might be at the root level in older JSONs or direct from AI
    overall_match_score: Optional[float] = None
    recommended_positions: Optional[List[str]] = Field(default_factory=list)
    analysis_summary: Optional[str] = None # Added as it's in the user's JSON

    # The nested models, now potentially constructed by root_validator
    intended_role_analysis: Optional[IntendedRoleAnalysis] = None
    human_ai_collaboration_fit: Optional[HumanAICollaborationFit] = None # Make optional, will be handled by root_validator

    strengths: List[str] = Field(default_factory=list)
    weaknesses_or_risks: List[str] = Field(default_factory=list)
    interview_questions: List[str] = Field(default_factory=list)
    overall_recommendation: Optional[str] = None # Optional, as we infer if missing
    next_steps_suggestion: Optional[str] = None

    @validator('applicant_name')
    def sanitize_name(cls, v):
        """Sanitizes applicant name (e.g., removes leading/trailing whitespace)."""
        return v.strip()

    @root_validator(pre=True)
    def preprocess_input(cls, values):
        # Handle human_ai_collaboration_fit being a direct number
        if 'human_ai_collaboration_fit' in values and isinstance(values['human_ai_collaboration_fit'], (int, float)):
            values['human_ai_collaboration_fit'] = {
                "assessment": values['human_ai_collaboration_fit'],
                "reason": "根据匹配度推断" # Default reason
            }
        
        # Handle intended_role_analysis being missing but data present at root
        if 'intended_role_analysis' not in values:
            root_score = values.get('overall_match_score')
            recommended_pos = values.get('recommended_positions')

            if root_score is not None:
                values['intended_role_analysis'] = {
                    "role_name": recommended_pos[0] if recommended_pos else "未知岗位",
                    "overall_match_score": root_score,
                    "score_reason": values.get('analysis_summary', '根据AI报告推断'), # Use analysis_summary if available
                    "preliminary_skills_match": {
                        "overall_assessment": "未知",
                        "details": []
                    },
                    "full_state_potential_analysis": {
                        "overall_assessment": "未知",
                        "details": []
                    }
                }
            else:
                # If no root score and no intended_role_analysis, provide a minimal default
                values['intended_role_analysis'] = {
                    "role_name": "未知岗位",
                    "overall_match_score": 0.0,
                    "score_reason": "数据缺失",
                    "preliminary_skills_match": {"overall_assessment": "未知", "details": []},
                    "full_state_potential_analysis": {"overall_assessment": "未知", "details": []}
                }
        return values

# --- Constants and Helper Functions ---
RECOMMENDATION_ORDER = ["强力推荐", "推荐", "考虑", "暂不推荐"]
FILTER_STATUS_OPTIONS = ["所有状态", "第一轮通过", "待面试", "不推荐"]
ROLES = [
    "AI产品运营官", "AI算法工程师", "AI交互与支持专家",
    "AI时代数字设计师", "全栈开发工程师", "数据科学家",
    "项目管理专家", "AI产品经理", "AI解决方案架构师",
    "AI伦理与合规专家", "商业拓展/销售总监", "AI培训师/技术传道师",
    "AI研究科学家/前沿探索者", "MLOps工程师", "数据治理与架构专家",
    "用户体验（UX）研究员", "市场与增长专家", "社区运营经理",
    "法律与伦理专家"
]

@lru_cache(maxsize=50) # Cache recommendation level inference
def infer_recommendation_level(match_score: float) -> str:
    """Infers recommendation level based on match score."""
    if match_score >= 85:
        return "强力推荐"
    elif match_score >= 70:
        return "推荐"
    elif match_score >= 50:
        return "考虑"
    else:
        return "暂不推荐"

# --- Performance Optimization: Cached File Processing ---
from concurrent.futures import ThreadPoolExecutor # Moved import to top, but keeping here for context
@st.cache_data(ttl=config.SESSION_TIMEOUT)  # Cache results for 1 hour, using config
def process_and_validate_json_content(file_contents_bytes: List[bytes], filenames: List[str]) -> List[Dict[str, Any]]:
    """
    Processes and validates multiple JSON file contents in parallel using a thread pool.
    Returns a list of validated raw data dictionaries, including their file hashes.
    """
    processed_data = []
    # Use ThreadPoolExecutor for CPU-bound tasks like JSON parsing and Pydantic validation
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
        futures = {executor.submit(_safe_json_load_sync, content_bytes, name): name for content_bytes, name in zip(file_contents_bytes, filenames)}
        for future in futures:
            name = futures[future]
            try:
                data, message, file_hash = future.result() # Now returns data, message, and file_hash
                if data:
                    # Add file_hash to the raw data for storage in DB
                    data['__file_hash__'] = file_hash 
                    processed_data.append(data)
                else:
                    st.error(ErrorHandler.handle_file_error(ValueError(message), name)) # Use ErrorHandler
            except Exception as e:
                st.error(ErrorHandler.handle_file_error(e, name))
                logger.error("Error in thread for file processing", filename=name, error=str(e))
    return processed_data

def _safe_json_load_sync(content_bytes: bytes, filename: str) -> Tuple[Dict[str, Any] | None, str, Optional[str]]:
    """
    Synchronous version of safe_json_load for ThreadPoolExecutor.
    Performs security validation, decodes, generates hash, and validates JSON structure.
    Returns (parsed_data, message, file_hash).
    """
    file_hash = None
    try:
        # 1. Validate file content using SecurityManager
        is_valid, validation_message = security.validate_file_content(content_bytes)
        if not is_valid:
            return None, validation_message, None

        # 2. Decode content to string (after initial validation)
        content_str = content_bytes.decode('utf-8')
        
        # 3. Generate file hash
        file_hash = security.generate_file_hash(content_str)

        # 4. Load JSON and validate with Pydantic
        data = json.loads(content_str)
        # Use CandidateReportModel to validate and transform the data
        validated_data = CandidateReportModel(**data) 
        
        return validated_data.dict(), "Success", file_hash # Return the validated/transformed dict
        
    except json.JSONDecodeError as e:
        return None, f"JSON format error: {str(e)}", file_hash
    except UnicodeDecodeError: # Should be caught by security.validate_file_content, but as a fallback
        return None, "Encoding error, please use UTF-8 encoding.", file_hash
    except ValidationError as e: # Catch Pydantic validation errors specifically
        return None, f"Data structure validation failed: {e.errors()}", file_hash
    except Exception as e: # Catch other unexpected errors
        return None, f"An unexpected error occurred during file processing: {str(e)}", file_hash


# --- Streamlit Session State (for UI state, not primary data storage) ---
# Session state is used for UI-specific ephemeral data like filter selections and current status.
# The primary data (candidate reports) are persisted in SQLite.
if 'candidate_statuses' not in st.session_state:
    st.session_state.candidate_statuses = {}
# 'raw_json_reports_for_sorting' is populated from DB for AI sorting, not directly from upload.
if 'raw_json_reports_for_sorting' not in st.session_state:
    st.session_state.raw_json_reports_for_sorting = []

# --- Streamlit UI ---
st.set_page_config(page_title="Signal Tower - 候选人排序系统", layout="wide", initial_sidebar_state="expanded")
st.title("🚦 Signal Tower | Genesis Insight 候选人排序系统")
st.markdown("上传多个结构化 JSON 报告，系统将自动提取候选人信息并进行排序分析。")

tab1, tab2, tab3 = st.tabs(["📋 候选人列表与筛选", "🧠 AI 智能排序引擎", "⚙️ 系统设置"])

with tab1:
    st.header("候选人列表与筛选")
    
    uploaded_files = st.file_uploader(
        "📤 上传候选人 JSON 文件（可多选）", 
        type="json", 
        accept_multiple_files=True,
        key="list_filter_uploader",
        help=f"最多上传 {config.MAX_FILES} 个文件，每个文件不超过 {config.MAX_FILE_SIZE//1024//1024}MB"
    )

    if uploaded_files:
        if len(uploaded_files) > config.MAX_FILES:
            st.error(f"文件数量超限，最多允许 {config.MAX_FILES} 个文件")
        else:
            file_contents_bytes = []
            file_names = []
            for file in uploaded_files:
                file_contents_bytes.append(file.read()) # Read content as bytes
                file_names.append(file.name)

            # Process files using the cached function
            with st.spinner("正在处理文件..."):
                processed_raw_data = process_and_validate_json_content(file_contents_bytes, file_names)
            
            if processed_raw_data:
                # Save each valid candidate to the database
                newly_processed_count = 0
                for raw_data_from_pydantic in processed_raw_data: # raw_data_from_pydantic is already a validated dict
                    applicant_name = raw_data_from_pydantic.get("applicant_name", "未知")
                    file_hash = raw_data_from_pydantic.pop('__file_hash__', None) # Get and remove hash before saving raw_data
                    if not file_hash:
                        st.error(f"无法为文件 {applicant_name} 生成哈希值，跳过保存。")
                        continue
                    try:
                        # Pass file_hash to save_candidate
                        db_manager.save_candidate(applicant_name, raw_data_from_pydantic, file_hash) 
                        if applicant_name not in st.session_state.candidate_statuses:
                            st.session_state.candidate_statuses[applicant_name] = "所有状态"
                        newly_processed_count += 1
                    except ValueError as e: # Catches "already exists" error from save_candidate
                        st.warning(f"跳过候选人 {applicant_name}: {e}")
                    except Exception as e:
                        st.error(ErrorHandler.handle_file_error(e, applicant_name)) # Log and display error
                
                if newly_processed_count > 0:
                    st.success(f"成功处理并加载 {newly_processed_count} 份候选人数据。")
                    st.rerun() # Rerun to refresh data from DB
                else:
                    st.info("没有新的有效候选人数据被加载。")
            else:
                st.warning("没有成功加载的有效候选人数据。请检查上传文件内容。")

    # Load all candidates from the database
    all_raw_candidates = db_manager.load_all_candidates() # Changed to load_all_candidates
    
    # Process raw data into a flat DataFrame format for display and filtering
    processed_for_df = []
    st.session_state.raw_json_reports_for_sorting = [] # Clear and repopulate for tab2 consistency
    for raw in all_raw_candidates:
        try:
            # Use Pydantic to parse and validate, ensuring consistent structure
            # This also handles type conversions and defaults as defined in the model
            validated_report = CandidateReportModel(**raw)
            candidate_name = validated_report.applicant_name
            
            # Extract main role safely from the validated report
            main_role = validated_report.intended_role_analysis.role_name if validated_report.intended_role_analysis else "未知岗位"
            
            # Extract human_ai_fit_value and prepare for display
            human_ai_fit_value = validated_report.human_ai_collaboration_fit.assessment if validated_report.human_ai_collaboration_fit else "未知"
            
            # Get recommendation level (prioritize 'overall_recommendation' if exists, else infer)
            recommendation_level = validated_report.overall_recommendation or \
                                   infer_recommendation_level(validated_report.intended_role_analysis.overall_match_score if validated_report.intended_role_analysis else 0)

            row = {
                "姓名": candidate_name,
                "岗位": main_role, 
                "匹配度": validated_report.intended_role_analysis.overall_match_score if validated_report.intended_role_analysis else 0,
                "人机契合度": human_ai_fit_value,
                "推荐等级": recommendation_level, 
                "风险数": len(validated_report.weaknesses_or_risks),
                "亮点": "、".join(validated_report.strengths),
                "原始风险": validated_report.weaknesses_or_risks,
                "面试建议": "、".join(validated_report.interview_questions),
                "原始报告": raw # Store original raw data for detail view
            }
            processed_for_df.append(row)
            # Only add validated data for AI sorting, removing _db_status and __file_hash__
            st.session_state.raw_json_reports_for_sorting.append(validated_report.dict()) # Use .dict() for Pydantic v1
            
            # Ensure status is loaded from DB or initialized from _db_status
            if candidate_name not in st.session_state.candidate_statuses:
                st.session_state.candidate_statuses[candidate_name] = raw.get('_db_status', '所有状态')

        except ValidationError as e:
            logger.error("Pydantic validation error for candidate from DB", name=raw.get('applicant_name', 'Unknown'), errors=e.errors())
            st.warning(f"跳过候选人 {raw.get('applicant_name', 'Unknown')} 由于数据结构不匹配。详情: {e.errors()[0]['msg']}")
        except Exception as e:
            logger.error("Error processing candidate for DataFrame from DB", name=raw.get('applicant_name', 'Unknown'), error=str(e))
            st.warning(f"跳过候选人 {raw.get('applicant_name', 'Unknown')} 由于数据处理错误。详情: {e}")

    df = pd.DataFrame(processed_for_df)

    if df.empty:
        st.info("没有成功加载的有效候选人数据。请上传 JSON 文件以开始分析。")
    else:
        # Sidebar filter and sort options
        st.sidebar.header("🎛️ 排序与筛选选项")
        
        selected_filter_status = st.sidebar.selectbox("按状态筛选", FILTER_STATUS_OPTIONS, key="filter_status_sidebar")
        sort_by = st.sidebar.selectbox("排序字段", ["匹配度", "风险数", "人机契合度", "推荐等级"], key="sort_by_sidebar")
        ascending = st.sidebar.checkbox("升序排列", value=False, key="ascending_sidebar")

        # Filter data
        filtered_df = df.copy()
        if selected_filter_status != "所有状态":
            filtered_names = [name for name, status in st.session_state.candidate_statuses.items() 
                              if status == selected_filter_status]
            filtered_df = filtered_df[filtered_df['姓名'].isin(filtered_names)]

        # Sorting logic optimization
        if not filtered_df.empty:
            if sort_by == "风险数":
                filtered_df = filtered_df.sort_values(by=sort_by, ascending=not ascending)
            elif sort_by == "推荐等级":
                filtered_df['推荐等级_ordered'] = pd.Categorical(
                    filtered_df['推荐等级'], 
                    categories=RECOMMENDATION_ORDER, 
                    ordered=True
                )
                filtered_df = filtered_df.sort_values(
                    by='推荐等级_ordered', 
                    ascending=ascending
                ).drop(columns=['推荐等级_ordered'])
            else:
                # Handle '人机契合度' which can be mixed type (float or string)
                if sort_by == "人机契合度":
                    # Convert to numeric for sorting, non-numeric become NaN
                    numeric_human_ai_fit = pd.to_numeric(filtered_df['人机契合度'], errors='coerce')
                    # Sort by numeric values first, then by original string for non-numeric
                    filtered_df = filtered_df.assign(
                        _sort_key=numeric_human_ai_fit
                    ).sort_values(
                        by=['_sort_key', '人机契合度'], # Sort by numeric, then by string for ties/non-numeric
                        ascending=[ascending, True] # Numeric by user choice, string always ascending
                    ).drop(columns=['_sort_key'])
                else:
                    filtered_df = filtered_df.sort_values(by=sort_by, ascending=ascending)

        # Candidate list display
        st.subheader(f"📋 候选人列表 (共 {len(filtered_df)} 人)")
        
        if filtered_df.empty:
            st.info("没有符合筛选条件的候选人。请调整筛选条件或上传更多数据。")
        else:
            for idx, (index, row) in enumerate(filtered_df.iterrows()):
                candidate_name = row['姓名']
                display_role = row['岗位'] if row['岗位'] != "未知岗位" else "未推荐岗位"
                
                with st.expander(f"#{idx+1} {candidate_name} | {display_role} | {row['匹配度']}% | {row['推荐等级']}"):
                    col1, col2 = st.columns([1, 2])
                    
                    with col1:
                        st.metric("匹配度", f"{row['匹配度']}%")
                        st.metric("风险数", f"{row['风险数']}个")
                        
                        human_ai_value = row['人机契合度']
                        human_ai_display = f"{human_ai_value}%" if isinstance(human_ai_value, (int, float)) else str(human_ai_value)
                        st.metric("人机契合度", human_ai_display)
                        
                        # Status selection
                        current_status = st.session_state.candidate_statuses.get(candidate_name, "所有状态")
                        new_status = st.selectbox(
                            "筛选状态", 
                            FILTER_STATUS_OPTIONS, 
                            index=FILTER_STATUS_OPTIONS.index(current_status),
                            key=f"status_{candidate_name}_{idx}" # Unique key
                        )
                        
                        if new_status != current_status:
                            db_manager.update_candidate_status(candidate_name, new_status) # Update status in DB
                            st.session_state.candidate_statuses[candidate_name] = new_status # Update session state
                            st.rerun() # Rerun to update display

                    with col2:
                        st.write("**亮点：**")
                        if row['亮点']:
                            for highlight in row['亮点'].split('、'):
                                if highlight.strip():
                                    st.write(f"• {highlight.strip()}")
                        else:
                            st.write("• None")
                            
                        st.write("**风险：**")
                        if row['原始风险']:
                            for risk in row['原始风险']:
                                st.write(f"• {risk}")
                        else:
                            st.write("• None")
                            
                        st.write("**面试建议：**")
                        if row['面试建议']:
                            for question in row['面试建议'].split('、'):
                                if question.strip():
                                    st.write(f"• {question.strip()}")
                        else:
                            st.write("• None")

        # Data visualization
        if not filtered_df.empty:
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("📊 岗位分布")
                role_counts = filtered_df["岗位"].value_counts()
                st.bar_chart(role_counts)
                
            with col2:
                st.subheader("📈 匹配度分布")
                st.bar_chart(filtered_df["匹配度"])

        # Export functionality
        st.subheader("📤 导出数据")
        if not filtered_df.empty:
            col1, col2 = st.columns(2)
            
            with col1:
                csv = filtered_df.drop(['原始报告', '原始风险'], axis=1).to_csv(index=False)
                st.download_button(
                    "下载 CSV 文件",
                    csv,
                    f"candidates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    "text/csv"
                )
            
            with col2:
                markdown_output = generate_markdown_report(filtered_df)
                st.download_button(
                    "下载 Markdown 报告",
                    markdown_output,
                    f"candidate_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                    "text/markdown"
                )

def generate_markdown_report(df: pd.DataFrame) -> str:
    """Generates a Markdown formatted report from the DataFrame."""
    markdown_output = f"# Candidate Analysis Report\n\n"
    markdown_output += f"Generation Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    markdown_output += f"Total Candidates: {len(df)}\n\n"
    
    for idx, (_, row) in enumerate(df.iterrows()):
        markdown_output += f"## {idx+1}. {row['姓名']}\n\n"
        markdown_output += f"- **Role**: {row['岗位']}\n"
        markdown_output += f"- **Match Score**: {row['匹配度']}%\n"
        markdown_output += f"- **Recommendation Level**: {row['推荐等级']}\n"
        markdown_output += f"- **Number of Risks**: {row['风险数']} ({'、'.join(row['原始风险']) if row['原始风险'] else 'None'})\n"
        markdown_output += f"- **Highlights**: {row['亮点'] or 'None'}\n"
        markdown_output += f"- **Interview Suggestions**: {row['面试建议'] or 'None'}\n\n"
        
    return markdown_output

with tab2:
    st.header("🧠 AI 智能排序引擎")
    st.markdown("基于深度分析的 AI 智能排序系统")
    
    # Reload raw reports from DB to ensure consistency, then use session state for display
    current_raw_reports_from_db = db_manager.load_all_candidates() # Changed to load_all_candidates
    # Filter out only the raw data for AI sorting, removing app-specific status and db metadata
    st.session_state.raw_json_reports_for_sorting = [
        # Use CandidateReportModel to ensure consistency for backend
        CandidateReportModel(**report).dict() 
        for report in current_raw_reports_from_db
    ]

    if not st.session_state.raw_json_reports_for_sorting:
        st.info("请先在「候选人列表与筛选」标签页上传 JSON 文件，或在此处重新上传。")
        
        # Provide independent upload option for sorting tab (reads as bytes)
        sorting_uploaded_files = st.file_uploader(
            "上传用于排序的 JSON 文件", 
            type="json", 
            accept_multiple_files=True,
            key="sorting_uploader",
            help=f"最多上传 {config.MAX_FILES} 个文件，每个文件不超过 {config.MAX_FILE_SIZE//1024//1024}MB"
        )
        
        if sorting_uploaded_files:
            file_contents_bytes = [file.read() for file in sorting_uploaded_files]
            file_names = [file.name for file in sorting_uploaded_files]
            
            with st.spinner("Processing files for sorting..."):
                processed_raw_data_for_sorting = process_and_validate_json_content(file_contents_bytes, file_names)
            
            if processed_raw_data_for_sorting:
                newly_processed_count = 0
                for raw_data_from_pydantic in processed_raw_data_for_sorting: # This is already a validated dict
                    applicant_name = raw_data_from_pydantic.get("applicant_name", "未知")
                    file_hash = raw_data_from_pydantic.pop('__file_hash__', None)
                    if not file_hash:
                        st.error(f"无法为文件 {applicant_name} 生成哈希值，跳过保存。")
                        continue
                    try:
                        db_manager.save_candidate(applicant_name, raw_data_from_pydantic, file_hash)
                        if applicant_name not in st.session_state.candidate_statuses:
                            st.session_state.candidate_statuses[applicant_name] = "所有状态"
                        newly_processed_count += 1
                    except ValueError as e:
                        st.warning(f"跳过候选人 {applicant_name}: {e}")
                    except Exception as e:
                        st.error(ErrorHandler.handle_file_error(e, applicant_name))
                
                if newly_processed_count > 0:
                    st.success(f"成功处理并加载 {newly_processed_count} 份候选人数据到数据库。")
                    st.rerun() # Rerun to refresh data from DB and update session state
                else:
                    st.info("没有新的有效候选人数据被加载用于排序。")
            else:
                st.warning("没有成功加载的有效候选人数据用于排序。")
    
    if st.session_state.raw_json_reports_for_sorting:
        st.success(f"已加载 {len(st.session_state.raw_json_reports_for_sorting)} 份候选人报告")
        
        selected_role = st.selectbox("选择目标岗位", ROLES, key="ai_sorting_role")
        
        col1, col2 = st.columns([1, 3])
        with col1:
            run_sorting = st.button(
                "🚀 开始 AI 排序", 
                disabled=len(st.session_state.raw_json_reports_for_sorting) < 2,
                type="primary"
            )
        
        with col2:
            if len(st.session_state.raw_json_reports_for_sorting) < 2:
                st.warning("至少需要2份报告才能进行排序")
        
        if run_sorting:
            with st.spinner("AI 正在进行智能分析排序..."):
                try:
                    # Use APIClient for sorting
                    result = api_client.sort_candidates(
                        data={
                            "candidate_reports": st.session_state.raw_json_reports_for_sorting,
                            "role": selected_role
                        }
                    )
                    
                    if result and "sorted_list" in result:
                        st.success("✅ AI 排序完成！")
                        display_ai_sorting_results(result["sorted_list"], selected_role)
                    else:
                        st.error("AI 排序失败：未收到有效结果")
                        
                except requests.exceptions.Timeout:
                    st.error("Request timed out. Please try again later.")
                except requests.exceptions.ConnectionError:
                    st.error(f"Could not connect to AI service ({config.FASTAPI_BACKEND_URL}). Please check your network connection.")
                except requests.exceptions.HTTPError as e:
                    st.error(f"AI service returned an error: {e}")
                except Exception as e:
                    logger.error("AI sorting process exception", error=str(e))
                    st.error("An unknown error occurred during sorting. Please try again.")

def display_ai_sorting_results(sorted_list: str, role: str):
    """Displays AI sorting results in a structured format."""
    st.subheader(f"🎯 {role} - AI 排序结果")
    
    # Parse Markdown result
    candidate_blocks = re.findall(r"(### 候选人姓名: .*?)(?=(?:### 候选人姓名: )|$)", sorted_list, re.DOTALL)
    
    if candidate_blocks:
        for i, block in enumerate(candidate_blocks):
            # Extract information
            name_match = re.search(r"### 候选人姓名: (.*)", block)
            match_score_match = re.search(r"- 综合匹配度: (.*?)%", block)
            recommendation_match = re.search(r"- 推荐等级: (.*)", block)
            
            name = name_match.group(1).strip() if name_match else "Unknown"
            match_score = match_score_match.group(1).strip() if match_score_match else "Unknown"
            recommendation = recommendation_match.group(1).strip() if recommendation_match else "Unknown"
            
            # Use card-like display
            with st.container(border=True): # Adds a border around each candidate block
                st.markdown(f"### 🏆 #{i+1} {name}")
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("综合匹配度", f"{match_score}%")
                with col2:
                    st.metric("推荐等级", recommendation)
                with col3:
                    st.metric("排名", f"#{i+1}")
                
                # Expand to show detailed analysis
                with st.expander("查看详细分析"):
                    st.markdown(block)
        
        # Provide download option
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        st.download_button(
            "📥 下载完整排序报告",
            sorted_list,
            f"ai_sorting_{role}_{timestamp}.md",
            "text/markdown",
            key="download_ai_report"
        )
    else:
        st.warning("未能解析 AI 排序结果。显示原始内容：")
        st.text(sorted_list)

with tab3:
    st.header("⚙️ 系统设置")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🔗 API 配置")
        st.info(f"当前后端 API 地址: `{config.FASTAPI_BACKEND_URL}`")
        st.markdown("要修改 API 地址，请在您的 Streamlit Cloud 设置中设置 `FASTAPI_BACKEND_URL` 环境变量。")
        
        if st.button("测试 API 连接"):
            try:
                status_code, response_json = api_client.test_connection(endpoint="/")
                if status_code == 200 and response_json.get("message") == "Genesis Insight Backend API is running!":
                    st.success("✅ API 连接正常！")
                else:
                    st.error(f"❌ API 返回意外响应: 状态 {status_code}, 响应: {response_json}")
            except requests.exceptions.Timeout:
                st.error("❌ API 连接超时。")
            except requests.exceptions.ConnectionError:
                st.error("❌ 无法连接到 API。请检查 URL 和网络。")
            except Exception as e:
                st.error(f"❌ API 测试期间发生错误: {str(e)}")
    
    with col2:
        st.subheader("📊 系统状态")
        # Load data directly from DB to show actual loaded count
        current_db_candidates = db_manager.load_all_candidates() # Changed to load_all_candidates
        st.metric("已加载候选人 (数据库)", len(current_db_candidates))
        st.metric("可用于 AI 排序的报告", len(st.session_state.raw_json_reports_for_sorting))
        
        if st.button("🗑️ 清空所有数据", type="secondary", help="这将从数据库中删除所有候选人数据。"):
            db_manager.clear_all_data()
            st.session_state.candidate_statuses = {}
            st.session_state.raw_json_reports_for_sorting = [] # Clear session state data
            st.success("所有数据已清空。")
            st.rerun()

# Footer
st.markdown("---")
st.markdown(
    """
    <div style='text-align: center; color: #666; font-size: 14px;'>
    Signal Tower - Genesis Insight 候选人排序系统 | 
    构建智能招聘解决方案 | 
    Version 2.0 (生产级增强)
    </div>
    """, 
    unsafe_allow_html=True
)
