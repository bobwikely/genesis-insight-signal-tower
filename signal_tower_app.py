import streamlit as st
import json
import pandas as pd
import requests # 用于调用 FastAPI 后端
from datetime import datetime # 用于生成唯一文件名
import re # 引入正则表达式模块，用于解析AI返回的Markdown

# 配置页面，设置标题和布局为宽屏
st.set_page_config(page_title="Signal Tower - 候选人排序系统", layout="wide")
# 应用主标题
st.title("🚦 Signal Tower | Genesis Insight 候选人排序系统")

# 应用简介
st.markdown("上传多个结构化 JSON 报告，系统将自动提取候选人信息并进行排序分析。")

# FastAPI 后端 API 的基础 URL
# IMPORTANT: 部署到生产环境时，请将其替换为您的后端API的实际生产URL！
FASTAPI_BACKEND_URL = "https://genesis-insight-fastapi-backend.onrender.com" # <--- 这里已更新为您的Render.com URL

# 定义推荐等级的排序顺序，用于后续自定义排序
RECOMMENDATION_ORDER = ["强力推荐", "推荐", "考虑", "暂不推荐"]
# 定义筛选状态选项
FILTER_STATUS_OPTIONS = ["所有状态", "第一轮通过", "待面试", "不推荐"]
# 终极版 18 个岗位类别 (与后端和简历透镜前端保持一致)
ROLES = [
    "AI产品运营官", "AI算法工程师", "AI交互与支持专家",
    "AI时代数字设计师", "全栈开发工程师", "数据科学家",
    "项目管理专家", "AI产品经理", "AI解决方案架构师",
    "AI伦理与合规专家", "商业拓展/销售总监", "AI培训师/技术传道师",
    "AI研究科学家/前沿探索者", "MLOps工程师", "数据治理与架构专家",
    "用户体验（UX）研究员", "市场与增长专家", "社区运营经理",
    "法律与伦理专家"
]

# Streamlit Session State 用于存储筛选状态和上传的JSON数据，以便在页面刷新时保持
if 'candidate_statuses' not in st.session_state:
    st.session_state.candidate_statuses = {}
if 'uploaded_json_data' not in st.session_state:
    st.session_state.uploaded_json_data = [] # 存储用于列表和筛选的扁平化数据
if 'raw_json_reports_for_sorting' not in st.session_state:
    st.session_state.raw_json_reports_for_sorting = [] # 存储原始JSON报告用于AI排序

# 创建标签页
tab1, tab2 = st.tabs(["📋 候选人列表与筛选", "🧠 AI 智能排序引擎"])

with tab1:
    st.header("候选人列表与筛选")
    # 文件上传组件，允许上传多个 JSON 文件
    uploaded_files = st.file_uploader(
        "📤 上传候选人 JSON 文件（可多选）", 
        type="json", 
        accept_multiple_files=True,
        key="list_filter_uploader" # 确保key唯一
    )

    if uploaded_files:
        current_upload_data = []
        current_raw_json_for_sorting = []
        for file in uploaded_files:
            try:
                raw = json.load(file) # 解析 JSON 文件内容
                
                # 检查必要的键是否存在，防止 KeyError
                # 新的Prompt B结构更扁平，检查 top-level keys
                if not all(k in raw for k in ["overall_match_score", "human_ai_collaboration_fit", "recommended_positions"]):
                    st.error(f"文件 {file.name} 缺少核心分析数据（如匹配度、人机契合度、推荐岗位）。请检查 JSON 结构是否符合「星辰B」输出格式。")
                    continue # 跳过此文件

                candidate_name = raw.get("applicant_name", "未知")
                
                # 从 JSON 中提取关键信息，构建成一行数据
                row = {
                    "姓名": candidate_name,
                    # 注意：新Prompt B的recommended_positions是列表，取第一个作为主岗位
                    "岗位": raw["recommended_positions"][0] if raw.get("recommended_positions") else "未知岗位", 
                    "匹配度": int(raw.get("overall_match_score", 0)),
                    "人机契合度": raw.get("human_ai_collaboration_fit", "未知"),
                    # 新Prompt B没有overall_recommendation，可以用recommended_positions的第一个作为推荐等级的简略表示
                    "推荐等级": raw["recommended_positions"][0] if raw.get("recommended_positions") else "未知", 
                    "风险数": len(raw.get("weaknesses_or_risks", [])), # 计算风险数量
                    "亮点": "、".join(raw.get("strengths", [])), # 将亮点列表用顿号连接
                    "原始风险": raw.get("weaknesses_or_risks", []), # 保留原始风险列表
                    "面试建议": "、".join(raw.get("interview_questions", [])), # 将面试问题也列出
                    "原始报告": raw # 保存原始JSON报告，以便在详情中显示
                }
                current_upload_data.append(row) # 将处理后的数据添加到列表中
                current_raw_json_for_sorting.append(raw) # 将原始JSON添加到用于排序的列表中

                # 初始化或更新候选人的筛选状态
                if candidate_name not in st.session_state.candidate_statuses:
                    st.session_state.candidate_statuses[candidate_name] = "所有状态" # 默认状态
                
            except json.JSONDecodeError:
                st.error(f"文件 {file.name} 不是有效的 JSON 格式。请确保文件内容是纯粹的 JSON。")
            except Exception as e:
                st.error(f"文件 {file.name} 加载失败: {e}")
        
        # 将当前上传的数据添加到 session_state 中，并去重
        # 简单的基于姓名去重（如果姓名相同，认为重复）
        # 先将所有现有数据和新数据合并
        combined_data = st.session_state.uploaded_json_data + current_upload_data
        combined_raw_json = st.session_state.raw_json_reports_for_sorting + current_raw_json_for_sorting

        # 使用字典进行去重，以姓名作为键，保留最新的数据
        unique_processed_data = {d['姓名']: d for d in combined_data}.values()
        unique_raw_reports = {report.get('applicant_name', '未知'): report for report in combined_raw_json}.values()

        st.session_state.uploaded_json_data = list(unique_processed_data)
        st.session_state.raw_json_reports_for_sorting = list(unique_raw_reports)


    # 将所有数据转换为 Pandas DataFrame，便于处理和显示
    df = pd.DataFrame(st.session_state.uploaded_json_data)

    # --- 健壮性检查：如果DataFrame为空，则停止后续操作 ---
    if df.empty:
        st.warning("没有成功加载的有效候选人数据。请上传 JSON 文件以开始分析。")
    else: # <--- 这个 else 语句的缩进是正确的
        # --- 筛选与排序设置 (侧边栏) ---
        st.sidebar.header("🎛️ 排序与筛选选项")
        
        # 筛选状态下拉框
        selected_filter_status = st.sidebar.selectbox("按状态筛选", FILTER_STATUS_OPTIONS)

        # 排序字段选择框
        # 注意：推荐等级现在是岗位名称，排序逻辑需要调整或移除
        sort_by = st.sidebar.selectbox("排序字段", ["匹配度", "风险数", "人机契合度"]) 
        # 升序/降序选择框
        ascending = st.sidebar.checkbox("升序排列", value=False) # 默认降序（匹配度、推荐等级高者优先，风险数低者优先）

        # 根据筛选状态过滤 DataFrame
        filtered_df = df.copy()
        if selected_filter_status != "所有状态":
            # 过滤出符合状态的候选人
            filtered_names = [name for name, status in st.session_state.candidate_statuses.items() if status == selected_filter_status]
            filtered_df = filtered_df[filtered_df['姓名'].isin(filtered_names)]

        # 根据选择的字段和顺序进行排序
        if sort_by == "风险数":
            # 风险数希望升序（风险越少越好），除非用户明确勾选了升序排列
            filtered_df = filtered_df.sort_values(by=sort_by, ascending=True if not ascending else False)
        else:
            # 其他数值字段，根据用户勾选的升序/降序
            filtered_df = filtered_df.sort_values(by=sort_by, ascending=ascending)

        # --- 候选人卡片可视化显示 ---
        st.subheader("📋 候选人列表")
        if filtered_df.empty:
            st.info("没有符合筛选条件的候选人。请调整筛选条件或上传更多有效数据。")
        else:
            for index, row in filtered_df.iterrows():
                candidate_name = row['姓名']
                # 使用 expander 结构，标题包含关键信息
                # 推荐等级现在是推荐岗位，所以标题显示推荐岗位
                display_role = row['岗位'] if row['岗位'] != "未知岗位" else "未推荐岗位"
                with st.expander(f"候选人：{candidate_name} | 岗位：{display_role} | 匹配度：{row['匹配度']}% | 推荐：{row['推荐等级']}"):
                    col1, col2 = st.columns([1, 2]) # 分两列显示

                    with col1:
                        st.markdown(f"**岗位：** {display_role}")
                        st.markdown(f"**匹配度：** {row['匹配度']}%")
                        st.markdown(f"**人机契合度：** {row['人机契合度']}%") # 人机契合度现在是数字
                        st.markdown(f"**推荐等级：** {row['推荐等级']}") # 这里显示的是推荐岗位
                        st.markdown(f"**风险数：** {row['风险数']}个")
                        
                        # 筛选状态下拉框，与 session_state 绑定
                        current_status = st.session_state.candidate_statuses.get(candidate_name, "所有状态")
                        # 使用一个临时的 key 来避免在 rerun 时的警告，同时保留候选人名称作为识别
                        unique_key = f"status_select_{candidate_name}_{index}" 
                        new_status = st.selectbox(
                            f"筛选状态 ({candidate_name})", 
                            FILTER_STATUS_OPTIONS, 
                            index=FILTER_STATUS_OPTIONS.index(current_status),
                            key=unique_key # 必须有唯一的key
                        )
                        # 如果状态改变，更新 session_state
                        if new_status != current_status:
                            st.session_state.candidate_statuses[candidate_name] = new_status
                            st.experimental_rerun() # 状态改变后重新运行应用以更新显示

                    with col2:
                        st.markdown(f"**亮点：**\n- {row['亮点'].replace('、', '\n- ')}")
                        st.markdown(f"**风险：**\n- {'\n- '.join(row['原始风险']) if row['原始风险'] else '无'}")
                        st.markdown(f"**面试建议：**\n- {row['面试建议'].replace('、', '\n- ')}") # 面试建议现在也是列表，用顿号连接
                        # 显示原始JSON报告的详情
                        st.markdown("---")
                        st.markdown("**原始JSON报告摘要：**")
                        st.json(row['原始报告']) # 直接显示原始JSON，方便调试和查看完整结构

        # --- 可视化模块 ---
        st.subheader("📊 岗位分布图")
        # 显示各岗位候选人的数量柱状图
        st.bar_chart(df["岗位"].value_counts())

        st.subheader("📈 匹配度分布图")
        # 显示匹配度的折线图（按匹配度降序排列）
        st.line_chart(df["匹配度"].sort_values(ascending=False).reset_index(drop=True))

        # --- 导出与协作 ---
        st.subheader("导出与协作")
        if st.button("📝 导出排序列表为 Markdown", key="export_markdown_tab1"):
            markdown_output = "## 候选人排序列表\n\n"
            for index, row in filtered_df.iterrows():
                markdown_output += f"### 候选人：{row['姓名']} | 岗位：{row['岗位']} | 匹配度：{row['匹配度']}% | 推荐：{row['推荐等级']}\n"
                markdown_output += f"- **亮点：** {row['亮点'].replace('、', ', ')}\n"
                markdown_output += f"- **风险：** {'、'.join(row['原始风险']) if row['原始风险'] else '无'}\n"
                markdown_output += f"- **面试建议：** {row['面试建议'].replace('、', ', ')}\n"
                markdown_output += f"- **筛选状态：** {st.session_state.candidate_statuses.get(row['姓名'], '未标记')}\n\n" # 导出时包含筛选状态
            st.download_button(
                label="下载 Markdown 报告",
                data=markdown_output,
                file_name="candidate_sorting_report.md",
                mime="text/markdown",
                key="download_markdown_tab1"
            )
        
    else:
        st.info("请上传至少一份结构化 JSON 报告以开始分析。")

with tab2:
    st.header("AI 智能排序引擎")
    st.markdown("上传多份候选人深度分析报告（JSON格式），AI 将为您生成综合排序列表。")
    st.info("提示：您在「候选人列表与筛选」标签页上传的JSON文件也会自动用于此处的AI智能排序。")

    # 在这个标签页再次提供文件上传，但这次是用于排序
    # 注意：这里我们使用 raw_json_reports_for_sorting，它包含了所有上传的原始JSON数据
    # 这样可以避免重复上传，并确保两个标签页的数据一致性
    current_sorting_uploaded_files = st.file_uploader(
        "📤 再次上传候选人 JSON 分析报告（可多选，将覆盖此标签页现有数据）", 
        type="json", 
        accept_multiple_files=True,
        key="sorting_uploader" # 确保key唯一
    )

    if current_sorting_uploaded_files:
        # 清空 session_state.raw_json_reports_for_sorting，只保留当前上传的
        # 否则每次上传都会累加，导致重复
        st.session_state.raw_json_reports_for_sorting = [] 
        for file in current_sorting_uploaded_files:
            try:
                report_data = json.load(file)
                st.session_state.raw_json_reports_for_sorting.append(report_data)
            except json.JSONDecodeError:
                st.error(f"文件 {file.name} 不是有效的 JSON 格式。请确保文件内容是纯粹的 JSON。")
            except Exception as e:
                st.error(f"文件 {file.name} 加载失败: {e}")
        
    if not st.session_state.raw_json_reports_for_sorting: # 检查 session_state 中的数据
        st.warning("没有成功加载的有效报告用于排序。请上传 JSON 文件。")
            
    # 岗位选择，用于 AI 排序的上下文
    selected_sorting_role = st.selectbox(
        "请选择排序的岗位", 
        ROLES,
        key="sorting_role_select"
    )

    if st.button("🚀 运行 AI 智能排序", 
                 disabled=not st.session_state.raw_json_reports_for_sorting or len(st.session_state.raw_json_reports_for_sorting) < 2, # 至少2份报告才能排序
                 key="run_ai_sorting_button"):
        with st.spinner("AI 正在分析并排序候选人，请稍候..."):
            try:
                response = requests.post(
                    f"{FASTAPI_BACKEND_URL}/sort-candidates/",
                    json={
                        "candidate_reports": st.session_state.raw_json_reports_for_sorting, # 使用 session_state 中的原始数据
                        "role": selected_sorting_role
                    }
                )
                response.raise_for_status() # 如果状态码不是 2xx，则抛出异常
                sorted_data = response.json()
                
                if sorted_data and "sorted_list" in sorted_data:
                    st.subheader("✅ AI 智能排序结果")
                    
                    markdown_list = sorted_data["sorted_list"]
                    
                    # 提取整体标题和排序依据
                    overall_title_match = re.search(r"## 候选人最终排序列表 - 【(.*?)】", markdown_list)
                    criteria_match = re.search(r"### 排序依据：(.*?)。", markdown_list)
                    
                    if overall_title_match:
                        st.markdown(f"## {overall_title_match.group(0)}")
                    if criteria_match:
                        st.markdown(f"### {criteria_match.group(0)}")

                    # 正则表达式匹配每个候选人块
                    # 匹配 "### 候选人姓名: " 开头，到下一个 "### 候选人姓名: " 或字符串结尾
                    candidate_blocks = re.findall(r"(### 候选人姓名: .*?)(?=(?:### 候选人姓名: )|$)", markdown_list, re.DOTALL)

                    if candidate_blocks:
                        for i, block in enumerate(candidate_blocks):
                            # 从每个块中提取详细信息
                            name_match = re.search(r"### 候选人姓名: (.*)", block)
                            match_score_match = re.search(r"- 综合匹配度: (.*?)%", block)
                            recommendation_match = re.search(r"- 推荐等级: (.*)", block)
                            human_ai_fit_match = re.search(r"- 人机契合度: (.*)", block)
                            highlights_match = re.search(r"- 亮点: (.*)", block)
                            risks_match = re.search(r"- 风险: (.*)", block)

                            name = name_match.group(1).strip() if name_match else "未知姓名"
                            match_score = match_score_match.group(1).strip() if match_score_match else "未知"
                            recommendation = recommendation_match.group(1).strip() if recommendation_match else "未知"
                            human_ai_fit = human_ai_fit_match.group(1).strip() if human_ai_fit_match else "未知"
                            highlights = highlights_match.group(1).strip() if highlights_match else "无"
                            risks = risks_match.group(1).strip() if risks_match else "无"

                            # 使用 Streamlit expander 或 columns 进行美观展示
                            st.markdown("---") # 分隔线
                            with st.expander(f"**#{i+1} 候选人：{name}** | 匹配度：{match_score}% | 推荐：{recommendation}"):
                                st.markdown(f"**综合匹配度：** {match_score}%")
                                st.markdown(f"**推荐等级：** {recommendation}")
                                st.markdown(f"**人机契合度：** {human_ai_fit}%") # 假设人机契合度也是百分比
                                st.markdown(f"**亮点：**\n- {highlights.replace(', ', '\n- ')}")
                                st.markdown(f"**风险：**\n- {risks.replace(', ', '\n- ')}")
                                # 可以在这里添加一个按钮，链接到该候选人的原始详细报告（如果需要）
                                # 或者直接显示原始报告的摘要信息

                    else:
                        st.warning("AI未能解析出具体的候选人排序信息。请检查后端返回的Markdown格式。")
                    
                    # 提供下载 Markdown 排序列表的选项
                    st.download_button(
                        label="下载 AI 排序 Markdown 报告",
                        data=markdown_list,
                        file_name=f"ai_sorted_candidates_{selected_sorting_role}_{datetime.now().strftime('%Y%m%d%H%M%S')}.md",
                        mime="text/markdown",
                        key="download_ai_sorted_markdown"
                    )
                else:
                    st.error("AI 排序失败：未收到有效的排序列表。")
            except requests.exceptions.ConnectionError:
                st.error(f"无法连接到后端 API：{FASTAPI_BACKEND_URL}。请确保后端服务正在运行。")
            except requests.exceptions.HTTPError as http_err:
                st.error(f"后端 API 返回错误：{http_err} - {response.text}")
            except Exception as e:
                st.error(f"AI 智能排序过程中发生错误: {e}")
    
    if not st.session_state.raw_json_reports_for_sorting or len(st.session_state.raw_json_reports_for_sorting) < 2:
        st.info("请上传至少两份候选人 JSON 分析报告以进行 AI 智能排序。")
