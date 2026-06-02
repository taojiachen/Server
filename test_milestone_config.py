import asyncio
import json
import requests
import os
from dotenv import load_dotenv
from db_manager import AsyncMySQLManager
import config

# 加载 .env
load_dotenv()

# ---------- 1. 定义要发送的 JSON 数据 ----------
json_data = {
    "task_id": 1001,
    "task_name": "小满-家庭种植种蒜",
    "task_type": "节日节气",
    "solar_term": "小满",
    "start_date": "2026-05-20",
    "end_date": "2026-06-01",
    "background": "小满时节，古人云“小满动三车”，此时正是种植蒜苗的好时机。",
    "description": "1. 亲手种植一盆蒜苗；2. 记录每周生长情况；3. 完成三个里程碑问答。",
    "chapters": [
        {
            "chapter_id": 1,
            "title": "播下希望，静待萌芽",
            "day_range": "第1-7天",
            "guide": "同学们首先需要准备蒜瓣、花盆和土壤。...",
            "questions": [
                {"question_id": "q1_1", "content": "你观察到的第一根绿芽是什么形状、什么颜色的？它从土里哪个位置钻出来的？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q1_2", "content": "从播种到现在，你一共浇了几次水？每次浇多少？你是怎么判断需要浇水的？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q1_3", "content": "你和谁一起种这盆蒜？你们会怎样分工？比如谁来负责记录、浇水、测量等。", "point": ["观察力", "好奇心", "耐心"]}
            ]
        },
        {
            "chapter_id": 2,
            "title": "量一量，小苗在长高",
            "day_range": "第8-16天",
            "guide": "当蒜苗普遍出芽后，同学们需要开始定期测量高度。...",
            "questions": [
                {"question_id": "q2_1", "content": "请你比较今天测量的高度和两天前的高度，相差多少毫米？蒜苗生长速度快吗？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q2_2", "content": "观察蒜苗的叶片颜色和数量，与一周前相比有什么变化？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q2_3", "content": "测量时遇到了哪些困难？你是如何解决的？", "point": ["观察力", "好奇心", "耐心"]}
            ]
        },
        {
            "chapter_id": 3,
            "title": "收获与分享",
            "day_range": "第17天及以后",
            "guide": "当蒜苗长到一定高度后，可以收割并用于烹饪，记录收获的喜悦。",
            "questions": [
                {"question_id": "q3_1", "content": "你的蒜苗最终长到了多少厘米？一共收割了多少根？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q3_2", "content": "在整个种植过程中，你学到了哪些关于植物生长的知识？", "point": ["观察力", "好奇心", "耐心"]},
                {"question_id": "q3_3", "content": "你会把这次种植经验分享给谁？为什么？", "point": ["观察力", "好奇心", "耐心"]}
            ]
        }
    ]
}

# ---------- 2. 准备请求参数 ----------
# 请确保设备 MAC 已存在于数据库，或者使用一个测试 MAC
# 如果设备不存在，服务器会自动创建（通过 esp 连接或 upsert 逻辑）
DEVICE_MAC = os.getenv("DEVICE_MAC", "10-51-DB-84-C4-48")  # 使用环境变量或默认值

# 构造请求体（只提取 chapters 部分，并添加 device_mac）
request_body = {
    "device_mac": DEVICE_MAC,
    "chapters": json_data["chapters"]
}

# 服务器地址（根据您的配置修改）
# 如果使用 HTTPS 且证书是自签名，需要 verify=False
URL = "https://localhost:8443/api/milestone/config"

# ---------- 3. 发送 HTTP 请求 ----------
def send_http_request():
    print(f"发送 POST 请求到 {URL}")
    print(f"设备 MAC: {DEVICE_MAC}")
    # 忽略 SSL 证书验证（因为自签名）
    response = requests.post(URL, json=request_body, verify=False)
    print(f"HTTP 状态码: {response.status_code}")
    print(f"响应内容: {response.json()}")
    return response.ok

# ---------- 4. 查询数据库验证 ----------
async def verify_in_db():
    print("\n========== 验证数据库存储 ==========")
    db = AsyncMySQLManager(config.db_config)
    await db.ensure_database()
    await db.create_pool()
    await db.init_tables()  # 确保表存在

    device = await db.get_device_by_mac(DEVICE_MAC)
    if not device:
        print(f"设备 {DEVICE_MAC} 不存在于数据库，请先连接 ESP 或手动添加设备。")
        await db.close_pool()
        return

    device_id = device['id']
    rows = await db.fetchall(
        "SELECT milestone_number, question_count, questions FROM milestones WHERE device_id = %s ORDER BY milestone_number",
        (device_id,)
    )
    if not rows:
        print("该设备没有任何里程碑配置。")
    else:
        print(f"设备 {DEVICE_MAC} 的里程碑配置：")
        for row in rows:
            milestone_num = row['milestone_number']
            q_count = row['question_count']
            questions_json = row['questions']
            if questions_json:
                questions = json.loads(questions_json)
                print(f"  里程碑 {milestone_num}: {q_count} 个问题")
                for idx, q in enumerate(questions, 1):
                    print(f"    问题{idx}: {q}")
            else:
                print(f"  里程碑 {milestone_num}: 问题数量 {q_count}，但问题文本为空")

    await db.close_pool()

# ---------- 5. 主流程 ----------
if __name__ == "__main__":
    # 第一步：发送 HTTP 请求
    success = send_http_request()
    if not success:
        print("请求失败，无法继续验证数据库。请确保服务器已启动且 URL 正确。")
        exit(1)

    # 第二步：查询数据库验证
    asyncio.run(verify_in_db())