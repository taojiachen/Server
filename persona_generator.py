import os
import asyncio
import argparse
import re
from concurrent.futures import ThreadPoolExecutor
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


def build_persona_prompt(dialog_lines: list, task_stats: dict) -> str:
    if dialog_lines:
        dialog_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in dialog_lines])
    else:
        dialog_text = "暂无对话记录（孩子刚刚开始使用）"

    avg_health = task_stats.get("avg_health", 100) or 100
    avg_satiety = task_stats.get("avg_satiety", 100) or 100
    avg_cleanliness = task_stats.get("avg_cleanliness", 100) or 100

    overall_completion = round((avg_health + avg_satiety + avg_cleanliness) / 3, 1)

    health_on_time = task_stats.get("health_on_time", 0) or 0
    satiety_on_time = task_stats.get("satiety_on_time", 0) or 0
    cleanliness_on_time = task_stats.get("cleanliness_on_time", 0) or 0
    on_time_rate = round((health_on_time + satiety_on_time + cleanliness_on_time) / 3 * 100, 1)

    record_count = task_stats.get("record_count", 0) or 0

    if avg_health < avg_satiety and avg_health < avg_cleanliness:
        pref = "更关注宠物健康"
    elif avg_satiety < avg_cleanliness:
        pref = "更关注宠物饱食"
    elif avg_cleanliness < avg_satiety:
        pref = "更关注宠物清洁"
    else:
        pref = "均衡照顾宠物"

    task_data_text = (
        f"- 整体完成率：{overall_completion}%（基于健康/饱食/清洁度平均值）\n"
        f"- 按时完成率：{on_time_rate}%（各指标按时维持率）\n"
        f"- 任务类型偏好：{pref}\n"
        f"- 近期状态记录次数：{record_count} 次"
    )

    prompt = f"""【角色设定】
你是一位擅长用童话意象解读儿童心理的发展心理学家。
【任务】
我会给你一份儿童的AI对话记录和任务完成数据。请你先自行分析出：高频情感词、高频话题、性格特征与行为模式，然后生成一份可用于AI绘画转化的“儿童人物画像”。画像必须用连续、生动、具象的语言写出，略带诗意，返回的画像必须包含以下内容：
1. 核心性格（2-3个词 + 简短行为依据）
2. 情感底色（整体基调 + 反复出现的情绪关键词）
3. 兴趣宇宙（3-5个话题领域及其独特视角）
4. 行为风格（结合完成率/按时率/案例说明模式，如谨慎规划型、冲动探索型、完美主义型等）
5. 象征性视觉符号（最能代表孩子的3个具象象征物，如“一只抱着问号的机械狐狸”）
6. 异常行为预警（首先给出分析结果（状态：正常/异常），然后请指出具体的异常行为（例如系统检测到孩子情绪持续低落或者习惯出现倒退等）并给家长提出温暖的建议帮助家长及时干预）
【对话记录】
{dialog_text}

【任务数据】
{task_data_text}
"""
    return prompt


def extract_response_text(response) -> str:
    text = ""
    if response.output:
        for item in response.output:
            if hasattr(item, 'content'):
                for content_part in item.content:
                    if content_part.type == 'output_text':
                        text += content_part.text
    return text.strip()


async def summarize_with_history(mac: str, limit: int, db: AsyncMySQLManager):
    dialog_lines = await db.get_recent_messages_by_device(mac, limit)
    if not dialog_lines:
        print("没有对话记录，跳过总结。")
        return

    dialog_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in dialog_lines])

    conversation = await db.get_device_latest_conversation(mac)
    old_summary = ""
    if conversation and conversation.get('summary_context'):
        old_summary = conversation['summary_context']

    if old_summary:
        prompt = f"""你是一个专业的对话摘要助手。下面是一份关于小朋友的“熟人档案”以及最新对话记录。
请结合旧档案和新对话，更新这份档案，确保保留所有关键信息，并将新对话中出现的新信息（名字、爱好、重要事件等）补充进去。

旧档案：
{old_summary}

最新对话记录：
{dialog_text}

要求：
- 用第三人称，温暖自然，像一份亲切的熟人档案
- 只输出最终档案内容，不要任何标题或说明
- 字数控制在500字以内
- 如果某项信息对话中没有提及，不要编造
"""
    else:
        prompt = f"""你是一个专业的对话摘要助手。下面是一个儿童与AI助手之间的连续对话记录。
请提取并总结出以下关键信息，以便未来AI能继续亲切、连贯地与孩子互动：
- 孩子的姓名或昵称、年龄
- 性格特点、兴趣爱好
- 重要经历或事件
- 常用口头禅等

要求：第三人称，温暖自然，只输出总结，500字以内。

对话记录：
{dialog_text}"""

    print("📝 正在生成/更新对话上下文总结...")
    response = await async_create_response(
        model="deepseek-v3-2-251201",
        input_messages=[{"role": "user", "content": prompt}]
    )
    summary_text = extract_response_text(response)

    if not summary_text:
        print("❌ 总结生成失败，未得到有效文本。")
        return

    conv = await db.get_device_latest_conversation(mac)
    if not conv:
        print("设备尚无会话记录，正在创建新会话...")
        conv_id = await db.create_conversation(mac)
        conv = {'id': conv_id}
    else:
        conv_id = conv['id']

    await db.update_conversation_summary(conv_id, summary_text)
    print(f"✅ 新摘要已存入会话 {conv_id}。")


async def generate_persona_analysis(mac: str, db: AsyncMySQLManager, limit: int = 100):
    dialog_lines = await db.get_recent_messages_by_device(mac, limit)
    task_stats = await db.get_task_statistics(mac)

    prompt = build_persona_prompt(dialog_lines, task_stats)
    print("🎨 正在生成人物画像...")
    response = await async_create_response(
        model="deepseek-v3-2-251201",
        input_messages=[{"role": "user", "content": prompt}]
    )
    output_text = extract_response_text(response)

    if output_text:
        device = await db.get_device_by_mac(mac)
        if device:
            await db.update_persona(mac, output_text)
            print("🖼️ 人物画像已更新到数据库。")

            # 解析异常并广播
            alert_match = re.search(r'状态[：:]\s*(正常|异常)', output_text)
            if alert_match and alert_match.group(1) == '异常':
                pattern = r'异常行为预警[：:]\s*(.*?)(?=\n\d+\.|\Z)'
                match = re.search(pattern, output_text, re.DOTALL)
                alert_content = match.group(1).strip() if match else "检测到异常状态"
                alert_data = {
                    "type": "abnormal_alert",
                    "mac": mac,
                    "status": "异常",
                    "description": alert_content
                }
                if hasattr(generate_persona_analysis, 'http_server') and generate_persona_analysis.http_server:
                    await generate_persona_analysis.http_server.broadcast_alert(mac, alert_data)
        else:
            print(f"设备 {mac} 不存在，跳过画像存储。")
    else:
        print("❌ 人物画像生成失败。")


async def summarize_dialog_context(mac: str, limit: int, db: AsyncMySQLManager):
    await summarize_with_history(mac, limit, db)


async def main():
    parser = argparse.ArgumentParser(description="生成儿童人物画像或总结对话上下文")
    parser.add_argument("--mac", type=str, default=os.getenv("DEVICE_MAC", ""),
                        help="目标设备的 MAC 地址")
    parser.add_argument("--limit", type=int, default=100,
                        help="获取最近多少条对话记录")
    parser.add_argument("--summarize", action="store_true",
                        help="总结对话上下文并存储到数据库，而非生成人物画像")
    args = parser.parse_args()

    if not args.mac:
        raise RuntimeError("请通过 --mac 或环境变量 DEVICE_MAC 指定设备 MAC 地址")

    db = AsyncMySQLManager(config.db_config)
    await db.ensure_database()
    await db.create_pool()
    await db.init_tables()

    try:
        if args.summarize:
            await summarize_dialog_context(args.mac, args.limit, db)
        else:
            await generate_persona_analysis(args.mac, db, args.limit)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())