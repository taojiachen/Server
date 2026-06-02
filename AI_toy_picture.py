import os
import asyncio
import base64
import aiohttp
import aiofiles
from dotenv import load_dotenv
from volcenginesdkarkruntime import Ark
from volcenginesdkarkruntime.types.images.images import SequentialImageGenerationOptions
from db_manager import AsyncMySQLManager
import config
from pathlib import Path
from persona_generator import async_create_response, extract_response_text

load_dotenv()

API_KEY = os.getenv('API_KEY')
if not API_KEY:
    raise RuntimeError("请在 .env 文件中设置 API_KEY")

image_client = Ark(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=API_KEY,
)

PICTURE_ROOT = "picture"


async def generate_image_prompt_from_evaluation(evaluation_text: str) -> str:
    """利用大语言模型将劳动评价转换为图像生成提示词"""
    instruction = (
        "你是一个专业的文生图提示词工程师。以下是一份关于儿童在劳动任务中的评价报告。"
        "请根据这份报告，生成一段详细的图像生成提示词（中文），用于 AI 绘画。\n\n"
        "【极其重要的要求】\n"
        "- 生成的图像必须与参考图片中的儿童为同一人，面部特征、发型、五官细节必须完全一致。\n"
        "- 只允许改变背景、动作、表情、肢体语言、服装（如果报告有提到可以调整）。\n"
        "- 提示词中必须强调“保持原图人脸不变”，例如“same face as the reference image”。\n\n"
        "根据评价报告中提到的劳动观念、劳动能力、劳动习惯和品质、劳动精神，\n"
        "设计儿童的表情、动作、肢体语言，以及相匹配的背景（如田园、农场、厨房等）。\n"
        f"评价报告：\n{evaluation_text}\n\n"
        "只输出提示词，不要附加任何解释。"
    )
    response = await async_create_response(
        model="deepseek-v3-2-251201",
        input_messages=[{"role": "user", "content": instruction}]
    )
    prompt = extract_response_text(response)
    if not prompt:
        raise ValueError("LLM 未能生成有效的图像提示词")
    return prompt


async def generate_milestone_image(mac: str, milestone_num: int, db_manager: AsyncMySQLManager, evaluation_text: str = None):
    """
    根据劳动评价报告生成里程碑对应的 AI 图片。
    如果 evaluation_text 为 None，则从 milestones 表中读取 assessment_evaluation。
    """
    device = await db_manager.get_device_by_mac(mac)
    if not device:
        raise ValueError(f"设备 {mac} 不存在")
    device_id = device['id']

    # 获取劳动评价文本
    if evaluation_text is None:
        row = await db_manager.fetchone(
            "SELECT assessment_evaluation FROM milestones WHERE device_id = %s AND milestone_number = %s",
            (device_id, milestone_num)
        )
        evaluation_text = row.get('assessment_evaluation', '').strip() if row else ''
        if not evaluation_text:
            raise ValueError(f"设备 {mac} 里程碑 {milestone_num} 没有劳动评价报告，无法生成图片")

    # 生成提示词
    prompt = await generate_image_prompt_from_evaluation(evaluation_text)

    safe_mac = mac.replace(':', '-')
    milestone_dir = Path(f"task/milestones{milestone_num}/{safe_mac}/AI_picture")
    milestone_dir.mkdir(parents=True, exist_ok=True)
    output_path = milestone_dir / f"milestones{milestone_num}.png"

    # 参考图片路径
    src_image_path = Path(f"picture/{safe_mac}/src/user.png")
    if not src_image_path.exists():
        raise FileNotFoundError(f"参考图片不存在: {src_image_path}")

    with open(src_image_path, "rb") as f:
        image_data = f.read()
    base64_image = base64.b64encode(image_data).decode('utf-8')
    data_url = f"data:image/png;base64,{base64_image}"

    print(f"🎨 正在为里程碑 {milestone_num} 生成 AI 图片...")
    imagesResponse = image_client.images.generate(
        model="doubao-seedream-4-0-250828",
        prompt=prompt,
        image=[data_url],
        size="2K",
        sequential_image_generation="auto",
        sequential_image_generation_options=SequentialImageGenerationOptions(max_images=1),
        response_format="url",
        watermark=True
    )

    if not imagesResponse.data:
        raise RuntimeError("API 未返回任何图片")

    image_url = imagesResponse.data[0].url
    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"下载图片失败 HTTP {resp.status}")
            image_bytes = await resp.read()
            async with aiofiles.open(output_path, 'wb') as f:
                await f.write(image_bytes)

    print(f"✅ 里程碑图片已保存: {output_path}")