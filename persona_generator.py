import os
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import List
from volcenginesdkarkruntime import Ark
from dotenv import load_dotenv
from db_manager import AsyncMySQLManager
import config

load_dotenv()

API_KEY = os.getenv('API_KEY')
if not API_KEY:
    raise RuntimeError("请在 .env 文件中设置 API_KEY")

client = Ark(
    base_url='https://ark.cn-beijing.volces.com/api/v3',
    api_key=API_KEY,
)

_executor = ThreadPoolExecutor(max_workers=2)


async def async_create_response(model: str, input_messages: list):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        lambda: client.responses.create(model=model, input=input_messages)
    )


def extract_response_text(response) -> str:
    text = ""
    if response.output:
        for item in response.output:
            if hasattr(item, 'content'):
                for content_part in item.content:
                    if content_part.type == 'output_text':
                        text += content_part.text
    return text.strip()


def build_labor_evaluation_prompt_for_milestone(questions: list, answers: dict, dialog_messages: List[dict]) -> str:
    """构建单里程碑劳动评价提示词，包含问答数据和当前里程碑的对话数据"""
    # 构建问答部分
    qa_pairs = []
    for idx, q in enumerate(questions, start=1):
        ans = answers.get(idx, "")
        if ans:
            qa_pairs.append(f"问题{idx}：{q}\n回答{idx}：{ans}")
    qa_text = "\n\n".join(qa_pairs) if qa_pairs else "（该里程碑尚无有效问答记录）"

    # 构建对话部分（仅当前里程碑，无需关键词过滤）
    if dialog_messages:
        dialog_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in dialog_messages])
    else:
        dialog_text = "（该里程碑期间无对话记录）"

    prompt = f"""# 角色设定
你是劳动教育评价专家，精通《义务教育劳动课程标准（2022年版）》中“农业生产劳动”任务群及《义务教育质量评价指标》中“劳动与社会实践”要求。

# 重要输出要求
1. 在评价报告中，**严禁使用“不足”、“缺点”、“错误”、“问题”等负面词汇**。对于需要改进的地方，请统一使用 **“待加强”、“可提升”、“建议关注”、“可进一步优化”** 等中性或积极表述。语言要温暖、鼓励、发展导向。
2. **输出格式要求**：请**不要输出任何标题行、元信息、评价专家、评价对象、评价周期、评价依据等内容**。直接以“## 一、总体评价”作为第一个输出行。严格按以下结构输出，从“一、总体评价”开始。

# 输入数据

## 1. 儿童对里程碑问题的回答
{qa_text}

## 2. 儿童与AI的对话记录（仅当前里程碑期间）
{dialog_text}

# 评价任务
请结合上述问答数据和对话数据，严格按照以下标准输出针对**当前里程碑**的劳动表现评价。输出结构如下（**禁止输出任何额外标题或元信息**）：

## 一、总体评价
（内容）

## 二、按核心素养四个维度详细评价
### 1. 劳动观念
- **等级：** 
- **证据：** 
- **分析与建议：** 

### 2. 劳动能力
- **等级：** 
- **证据：** 
- **分析与建议：** 

### 3. 劳动习惯和品质
- **等级：** 
- **证据：** 
- **分析与建议：** 

### 4. 劳动精神
- **等级：** 
- **证据：** 
- **分析与建议：** 

## 三、学段达成度评价
（内容）

## 四、结合《义务教育质量评价指标》
- **B11劳动习惯：** 
- **B12社会体验：** 

## 五、综合结论与改进建议
- **综合等级：** 
- **主要优势：** 
- **待加强方面：** 
- **三条具体建议（给教师/家长）：** 

# 输出要求
- 每条结论必须引用儿童原话作为证据。
- 语言专业、温暖、可操作。
- 避免任何贬低性词汇。
- **禁止输出“评价专家”、“评价对象”、“评价周期”、“评价依据”等元信息，禁止输出报告标题或任何分隔线（如“---”）。直接以“## 一、总体评价”开头。**
"""
    return prompt


async def generate_labor_evaluation_for_milestone(mac: str, milestone_number: int, db: AsyncMySQLManager):
    """
    基于指定里程碑的问答数据 + 该里程碑期间的对话数据，生成劳动评价报告，
    并更新 milestones 表中的 assessment_evaluation 字段。
    """
    device = await db.get_device_by_mac(mac)
    if not device:
        print(f"设备 {mac} 不存在")
        return
    device_id = device['id']

    # 获取里程碑的问题列表
    milestone_row = await db.fetchone(
        "SELECT questions FROM milestones WHERE device_id = %s AND milestone_number = %s",
        (device_id, milestone_number)
    )
    if not milestone_row or not milestone_row['questions']:
        print(f"设备 {mac} 里程碑 {milestone_number} 没有配置问题，跳过评价生成")
        return

    questions = json.loads(milestone_row['questions'])
    if not questions:
        return

    # 获取该里程碑的回答
    answers_rows = await db.fetchall(
        "SELECT question_index, answer_text FROM milestone_answers "
        "WHERE device_id = %s AND milestone_number = %s AND answer_text IS NOT NULL AND answer_text != '' "
        "ORDER BY question_index",
        (device_id, milestone_number)
    )
    answer_dict = {a['question_index']: a['answer_text'] for a in answers_rows}

    if not answer_dict:
        print(f"设备 {mac} 里程碑 {milestone_number} 没有有效回答，跳过评价生成")
        return

    # 获取该里程碑期间的对话记录（仅 current_milestone = milestone_number）
    dialog_messages = await db.get_messages_by_milestone(mac, milestone_number, limit=500)

    # 构建 prompt
    prompt = build_labor_evaluation_prompt_for_milestone(questions, answer_dict, dialog_messages)
    print(f"📝 正在为设备 {mac} 里程碑 {milestone_number} 生成劳动评价报告（含当前里程碑对话分析）...")

    # 调用 AI
    response = await async_create_response(
        model="deepseek-v3-2-251201",
        input_messages=[{"role": "user", "content": prompt}]
    )
    evaluation_text = extract_response_text(response)

    if not evaluation_text:
        print("❌ 劳动评价生成失败，未得到有效文本。")
        return

    # 更新 milestones 表中的 assessment_evaluation 字段
    await db.execute(
        "UPDATE milestones SET assessment_evaluation = %s WHERE device_id = %s AND milestone_number = %s",
        (evaluation_text, device_id, milestone_number)
    )
    print(f"✅ 劳动评价报告已保存到设备 {mac} 里程碑 {milestone_number}")