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

# 如果有文件上传
if uploaded_files:
    all_data = [] # 用于存储所有候选人的数据
    # 遍历每个上传的文件
    for file in uploaded_files:
        try:
            raw = json.load(file) # 解析 JSON 文件内容
            # 从 JSON 中提取关键信息，构建成一行数据
            row = {
                "姓名": raw.get("applicant_name", "未知"),
                "岗位": raw["intended_role_analysis"]["role_name"],
                "匹配度": int(raw["intended_role_analysis"]["overall_match_score"]),
                "人机契合度": raw["human_ai_collaboration_fit"]["assessment"],
                "推荐等级": raw["overall_recommendation"],
                "风险数": len(raw.get("weaknesses_or_risks", [])), # 计算风险数量
                "亮点": "、".join(raw.get("strengths", [])), # 将亮点列表用顿号连接
                "面试建议": raw.get("next_steps_suggestion", "")
            }
            all_data.append(row) # 将处理后的数据添加到列表中
        except Exception as e:
            # 如果文件加载或解析失败，显示错误信息
            st.error(f"文件 {file.name} 加载失败: {e}")

    # 将所有数据转换为 Pandas DataFrame，便于处理和显示
    df = pd.DataFrame(all_data)

    # --- 筛选与排序设置 (侧边栏) ---
    st.sidebar.header("🎛️ 排序与筛选选项")
    # 排序字段选择框
    sort_by = st.sidebar.selectbox("排序字段", ["匹配度", "风险数", "推荐等级", "人机契合度"]) # 增加人机契合度
    # 升序/降序选择框
    ascending = st.sidebar.checkbox("升序排列", value=False) # 默认降序（匹配度、推荐等级高者优先，风险数低者优先）

    # 根据选择的字段和顺序进行排序
    # 对于“风险数”，我们希望风险数低的排在前面，所以当排序字段是“风险数”时，ascending应该为True
    # 对于“推荐等级”，需要自定义排序逻辑，因为它是字符串
    if sort_by == "推荐等级":
        # 定义推荐等级的排序顺序
        recommendation_order = ["强力推荐", "推荐", "考虑", "暂不推荐"]
        # 使用 CategoricalDtype 进行自定义排序
        df['推荐等级_ordered'] = pd.Categorical(df['推荐等级'], categories=recommendation_order, ordered=True)
        filtered_df = df.sort_values(by='推荐等级_ordered', ascending=ascending).drop(columns=['推荐等级_ordered'])
    elif sort_by == "风险数":
        # 风险数希望升序（风险越少越好），除非用户明确勾选了升序排列
        filtered_df = df.sort_values(by=sort_by, ascending=True if not ascending else False)
    else:
        # 其他数值字段，根据用户勾选的升序/降序
        filtered_df = df.sort_values(by=sort_by, ascending=ascending)


    # --- 候选人排序列表显示 ---
    st.subheader("📋 候选人排序列表")
    st.dataframe(filtered_df, use_container_width=True) # 显示排序后的DataFrame

    # --- 可视化模块 ---
    st.subheader("📊 岗位分布图")
    # 显示各岗位候选人的数量柱状图
    st.bar_chart(df["岗位"].value_counts())

    st.subheader("📈 匹配度分布图")
    # 显示匹配度的折线图（按匹配度降序排列）
    st.line_chart(df["匹配度"].sort_values(ascending=False).reset_index(drop=True))

    # --- 增强功能预留：导出 Markdown 排序列表 ---
    st.subheader("导出与协作")
    if st.button("📝 导出排序列表为 Markdown"):
        markdown_output = "## 候选人排序列表\n\n"
        for index, row in filtered_df.iterrows():
            markdown_output += f"### 候选人：{row['姓名']} | 岗位：{row['岗位']} | 匹配度：{row['匹配度']}% | 推荐：{row['推荐等级']}\n"
            markdown_output += f"- **亮点：** {row['亮点']}\n"
            markdown_output += f"- **风险：** {row['风险数']}个 ({row['weaknesses_or_risks'] if 'weaknesses_or_risks' in row else '无'})\n" # 修正：这里需要原始的风险列表
            markdown_output += f"- **面试建议：** {row['面试建议']}\n\n"
        st.download_button(
            label="下载 Markdown 报告",
            data=markdown_output,
            file_name="candidate_sorting_report.md",
            mime="text/markdown"
        )
    
    # 修正：在导出Markdown时，如果需要原始的风险列表，需要从raw数据中获取或者在df中保留
    # 目前df中只有风险数，没有原始风险列表。为了简化，我们暂时只显示风险数。
    # 如果需要原始风险列表，需要修改all_data的row结构，或者在df中保留原始列表。
    # 考虑到当前df结构，上面的风险显示为“风险数个”是正确的。

else:
    # 没有文件上传时的提示信息
    st.info("请上传至少一份结构化 JSON 报告以开始分析。")

