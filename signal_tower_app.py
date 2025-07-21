import streamlit as st
import json
import pandas as pd

# 配置页面，设置标题和布局为宽屏
st.set_page_config(page_title="Signal Tower - 候选人排序系统", layout="wide")
# 应用主标题
st.title("🚦 Signal Tower | Genesis Insight 候选人排序系统")

# 应用简介
st.markdown("上传多个结构化 JSON 报告，系统将自动提取候选人信息并进行排序分析。")

# 文件上传组件，允许上传多个 JSON 文件
uploaded_files = st.file_uploader(
    "📤 上传候选人 JSON 文件（可多选）", 
    type="json", 
    accept_multiple_files=True
)

# 定义推荐等级的排序顺序，用于后续自定义排序
RECOMMENDATION_ORDER = ["强力推荐", "推荐", "考虑", "暂不推荐"]
# 定义筛选状态选项
FILTER_STATUS_OPTIONS = ["所有状态", "第一轮通过", "待面试", "不推荐"]

# Streamlit Session State 用于存储筛选状态，以便在页面刷新时保持
if 'candidate_statuses' not in st.session_state:
    st.session_state.candidate_statuses = {}

# 如果有文件上传
if uploaded_files:
    all_data = [] # 用于存储所有候选人的数据
    # 遍历每个上传的文件
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
            all_data.append(row) # 将处理后的数据添加到列表中

            # 初始化或更新候选人的筛选状态
            if candidate_name not in st.session_state.candidate_statuses:
                st.session_state.candidate_statuses[candidate_name] = "所有状态" # 默认状态
            
        except Exception as e:
            # 如果文件加载或解析失败，显示错误信息
            st.error(f"文件 {file.name} 加载失败: {e}")

    # 将所有数据转换为 Pandas DataFrame，便于处理和显示
    df = pd.DataFrame(all_data)

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
        st.info("没有符合筛选条件的候选人。")
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
                    new_status = st.selectbox(
                        f"筛选状态 ({candidate_name})", 
                        FILTER_STATUS_OPTIONS, 
                        index=FILTER_STATUS_OPTIONS.index(current_status),
                        key=f"status_{candidate_name}" # 必须有唯一的key
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
    if st.button("📝 导出排序列表为 Markdown"):
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
            mime="text/markdown"
        )
    
else:
    # 没有文件上传时的提示信息
    st.info("请上传至少一份结构化 JSON 报告以开始分析。")

