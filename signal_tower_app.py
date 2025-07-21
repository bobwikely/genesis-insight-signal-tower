import streamlit as st
import json
import pandas as pd
import requests # 用于调用 FastAPI 后端

# 配置页面，设置标题和布局为宽屏
st.set_page_config(page_title="Signal Tower - 候选人排序系统", layout="wide")
# 应用主标题
st.title("🚦 Signal Tower | Genesis Insight 候选人排序系统")

# 应用简介
st.markdown("上传多个结构化 JSON 报告，系统将自动提取候选人信息并进行排序分析。")

# FastAPI 后端 API 的基础 URL
# 在本地开发时，指向本地运行的FastAPI后端
# TODO: 部署到生产环境时，请将其替换为您的后端API的实际生产URL！
FASTAPI_BACKEND_URL = "http://localhost:8000"

# 定义推荐等级的排序顺序，用于后续自定义排序
RECOMMENDATION_ORDER = ["强力推荐", "推荐", "考虑", "暂不推荐"]
# 定义筛选状态选项
FILTER_STATUS_OPTIONS = ["所有状态", "第一轮通过", "待面试", "不推荐"]
# 定义岗位列表
ROLES = [
    "AI产品经理", "算法/AI工程师", "AI时代全栈工程师",
    "数据专家/助理", "AI产品运营官", "AI交互与支持专家", "AI时代数字设计师"
]

# Streamlit Session State 用于存储筛选状态，以便在页面刷新时保持
if 'candidate_statuses' not in st.session_state:
    st.session_state.candidate_statuses = {}
if 'uploaded_json_data' not in st.session_state:
    st.session_state.uploaded_json_data = []

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
        for file in uploaded_files:
            try:
                raw = json.load(file) # 解析 JSON 文件内容
                candidate_name = raw.get("applicant_name", "未知")
                
                # 从 JSON 中提取关键信息，构建成一行数据
                row = {
                    "姓名": candidate_name,
                    "岗位": raw["intended_role_analysis"]["role_name"],
                    "匹配度": int(raw["intended_role_analysis"]["overall_match_score"]),
                    "人机契合度": raw["human_ai_collaboration_fit"]["assessment"],
                    "推荐等级": raw["overall_recommendation"],
                    "风险数": len(raw.get("weaknesses_or_risks", [])), # 计算风险数量
                    "亮点": "、".join(raw.get("strengths", [])), # 将亮点列表用顿号连接
                    "原始风险": raw.get("weaknesses_or_risks", []), # 保留原始风险列表
                    "面试建议": raw.get("next_steps_suggestion", "")
                }
                current_upload_data.append(row) # 将处理后的数据添加到列表中

                # 初始化或更新候选人的筛选状态
                if candidate_name not in st.session_state.candidate_statuses:
                    st.session_state.candidate_statuses[candidate_name] = "所有状态" # 默认状态
                
            except json.JSONDecodeError:
                st.error(f"文件 {file.name} 不是有效的 JSON 格式。请确保文件内容是纯粹的 JSON。")
            except KeyError as ke:
                st.error(f"文件 {file.name} 缺少必要的键值：{ke}。请检查 JSON 结构是否完整。")
            except Exception as e:
                st.error(f"文件 {file.name} 加载失败: {e}")
        
        # 将当前上传的数据添加到 session_state 中，以便在不同标签页间共享
        st.session_state.uploaded_json_data.extend(current_upload_data)
        # 移除重复项，以防用户多次上传相同文件
        st.session_state.uploaded_json_data = [dict(t) for t in {tuple(d.items()) for d in st.session_state.uploaded_json_data}]

    # 将所有数据转换为 Pandas DataFrame，便于处理和显示
    df = pd.DataFrame(st.session_state.uploaded_json_data)

    # --- 健壮性检查：如果DataFrame为空，则停止后续操作 ---
    if df.empty:
        st.warning("没有成功加载的有效候选人数据。请上传 JSON 文件以开始分析。")
    else:
        # --- 筛选与排序设置 (侧边栏) ---
        st.sidebar.header("🎛️ 排序与筛选选项")
        
        # 筛选状态下拉框
        selected_filter_status = st.sidebar.selectbox("按状态筛选", FILTER_STATUS_OPTIONS)

        # 排序字段选择框
        sort_by = st.sidebar.selectbox("排序字段", ["匹配度", "风险数", "推荐等级", "人机契合度"]) 
        # 升序/降序选择框
        ascending = st.sidebar.checkbox("升序排列", value=False) # 默认降序（匹配度、推荐等级高者优先，风险数低者优先）

        # 根据筛选状态过滤 DataFrame
        filtered_df = df.copy()
        if selected_filter_status != "所有状态":
            # 过滤出符合状态的候选人
            filtered_names = [name for name, status in st.session_state.candidate_statuses.items() if status == selected_filter_status]
            filtered_df = filtered_df[filtered_df['姓名'].isin(filtered_names)]

        # 根据选择的字段和顺序进行排序
        if sort_by == "推荐等级":
            # 使用 CategoricalDtype 进行自定义排序
            filtered_df['推荐等级_ordered'] = pd.Categorical(filtered_df['推荐等级'], categories=RECOMMENDATION_ORDER, ordered=True)
            filtered_df = filtered_df.sort_values(by='推荐等级_ordered', ascending=ascending).drop(columns=['推荐等级_ordered'])
        elif sort_by == "风险数":
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
                with st.expander(f"候选人：{candidate_name} | 岗位：{row['岗位']} | 匹配度：{row['匹配度']}% | 推荐：{row['推荐等级']}"):
                    col1, col2 = st.columns([1, 2]) # 分两列显示

                    with col1:
                        st.markdown(f"**岗位：** {row['岗位']}")
                        st.markdown(f"**匹配度：** {row['匹配度']}%")
                        st.markdown(f"**人机契合度：** {row['人机契合度']}")
                        st.markdown(f"**推荐等级：** {row['推荐等级']}")
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
                        st.markdown(f"**面试建议：** {row['面试建议']}")

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
                markdown_output += f"- **面试建议：** {row['面试建议']}\n"
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

    # 在这个标签页再次提供文件上传，但这次是用于排序
    sorting_uploaded_files = st.file_uploader(
        "📤 上传候选人 JSON 分析报告（可多选）", 
        type="json", 
        accept_multiple_files=True,
        key="sorting_uploader" # 确保key唯一
    )

    candidate_reports_for_sorting = []
    if sorting_uploaded_files:
        for file in sorting_uploaded_files:
            try:
                report_data = json.load(file)
                candidate_reports_for_sorting.append(report_data)
            except json.JSONDecodeError:
                st.error(f"文件 {file.name} 不是有效的 JSON 格式。请确保文件内容是纯粹的 JSON。")
            except Exception as e:
                st.error(f"文件 {file.name} 加载失败: {e}")
        
        if not candidate_reports_for_sorting:
            st.warning("没有成功加载的有效报告用于排序。")
            
    # 岗位选择，用于 AI 排序的上下文
    selected_sorting_role = st.selectbox(
        "请选择排序的岗位", 
        ROLES,
        key="sorting_role_select"
    )

    if st.button("🚀 运行 AI 智能排序", disabled=not candidate_reports_for_sorting, key="run_ai_sorting_button"):
        with st.spinner("AI 正在分析并排序候选人，请稍候..."):
            try:
                response = requests.post(
                    f"{FASTAPI_BACKEND_URL}/sort-candidates/",
                    json={
                        "candidate_reports": candidate_reports_for_sorting,
                        "role": selected_sorting_role
                    }
                )
                response.raise_for_status() # 如果状态码不是 2xx，则抛出异常
                sorted_data = response.json()
                
                if sorted_data and "sorted_list" in sorted_data:
                    st.subheader("✅ AI 智能排序结果")
                    # 使用 st.markdown 渲染 AI 返回的 Markdown 列表
                    st.markdown(sorted_data["sorted_list"])
                    
                    # 提供下载 Markdown 排序列表的选项
                    st.download_button(
                        label="下载 AI 排序 Markdown 报告",
                        data=sorted_data["sorted_list"],
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
    
    if not sorting_uploaded_files:
        st.info("请上传至少一份候选人 JSON 分析报告以进行 AI 智能排序。")

