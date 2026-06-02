import asyncio
import websockets
import ssl
import os
import sys
import pyaudio
import json
import time
from queue import Queue, Empty
import threading
from typing import Dict, Any, Set, Optional, List
import ctypes
import struct
import logging
from dataclasses import dataclass
from pathlib import Path
import re
from speech_recognizer import recognize_audio

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('esp_audio_server.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

IMAGE_SAVE_DIR = "task/images"
Path(IMAGE_SAVE_DIR).mkdir(parents=True, exist_ok=True)

try:
    opus_dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'opus.dll')
    if os.path.exists(opus_dll_path):
        ctypes.CDLL(opus_dll_path)
        logger.info(f"✅ Opus DLL加载成功: {opus_dll_path}")
    else:
        logger.warning(f"⚠️  Opus DLL文件不存在: {opus_dll_path}")
except Exception as e:
    logger.error(f"❌ Opus DLL加载失败: {e}")

try:
    import opuslib
    from opuslib.exceptions import OpusError
    logger.info("✅ OpusLib导入成功")
except ImportError as e:
    logger.error(f"❌ OpusLib导入失败: {e}")
    sys.exit(1)

try:
    import protocol
    logger.info("✅ Protocol模块导入成功")
except ImportError as e:
    logger.warning(f"⚠️  Protocol模块导入失败: {e}")

# ==================== 音频参数 ====================
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_DURATION_MS = 60
OPUS_FRAME_SAMPLES = OPUS_SAMPLE_RATE * OPUS_FRAME_DURATION_MS // 1000

# ==================== 数据类 ====================
@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    format: int = pyaudio.paInt16
    chunk_size: int = 960
    buffer_size: int = 100
    min_buffer_frames: int = 10
    queue_warn_threshold: int = 45

@dataclass
class FrameHeader:
    magic1: int = 0xAA
    magic2: int = 0x55
    sequence: int = 0
    data_size: int = 0

class AudioFrameParser:
    @staticmethod
    def parse_frame_header(data: bytes) -> tuple[bool, str, Optional[FrameHeader], Optional[bytes]]:
        if len(data) < 6:
            return False, f"帧长度不足6字节（实际{len(data)}字节）", None, None
        magic1, magic2, seq_high, seq_low, size_high, size_low = struct.unpack('BBBBBB', data[:6])
        if magic1 != 0xAA or magic2 != 0x55:
            return False, f"帧头标识错误（0x{magic1:02X}{magic2:02X}）", None, None
        sequence = (seq_high << 8) | seq_low
        data_size = (size_high << 8) | size_low
        if len(data) != 6 + data_size:
            return False, f"数据长度不匹配（声明{data_size}字节，实际{len(data)-6}字节）", None, None
        payload = data[6:6+data_size]
        header = FrameHeader(magic1, magic2, sequence, data_size)
        return True, f"帧{sequence}校验通过（payload {data_size}字节）", header, payload

class OpusDecoder:
    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self.decoder = None
        self.consecutive_error_count = 0
        self.max_consecutive_errors = 5
        self.last_error_time = 0
        self.error_print_interval = 1.0
        self._init_decoder()

    def _init_decoder(self):
        try:
            self.decoder = opuslib.Decoder(self.sample_rate, self.channels)
            self.consecutive_error_count = 0
            logger.info(f"✅ Opus解码器初始化成功: {self.sample_rate}Hz, {self.channels}声道")
        except Exception as e:
            logger.error(f"❌ Opus解码器初始化失败: {e}")
            self.decoder = None

    def decode(self, opus_data: bytes, frame_size: int) -> Optional[bytes]:
        if not self.decoder:
            logger.error("❌ 解码器未初始化")
            return None
        try:
            pcm_data = self.decoder.decode(opus_data, frame_size)
            self.consecutive_error_count = 0
            logger.debug(f"✅ Opus解码成功: {len(opus_data)}→{len(pcm_data)}字节")
            return pcm_data
        except OpusError as e:
            self.consecutive_error_count += 1
            current_time = time.time()
            if current_time - self.last_error_time > self.error_print_interval:
                logger.error(f"❌ Opus解码错误: {e}, 连续错误数: {self.consecutive_error_count}")
                self.last_error_time = current_time
            if self.consecutive_error_count >= self.max_consecutive_errors:
                logger.warning("⚠️  连续解码错误，重置解码器")
                self._init_decoder()
            return None
        except Exception as e:
            logger.error(f"❌ 解码器异常: {e}")
            return None

class ESPWebSocketServer:
    def __init__(self, host: str = '0.0.0.0', port: int = 8765, use_ssl: bool = True,
                 cert_file: str = 'server.crt', key_file: str = 'server.key'):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.cert_file = cert_file
        self.key_file = key_file
        self.server = None
        self.active_connections: Set[websockets.WebSocketServerProtocol] = set()
        self.dialog_session = None
        self.db_manager = None

        self.audio_config = AudioConfig()
        self.frame_parser = AudioFrameParser()
        self.opus_decoder = OpusDecoder(self.audio_config.sample_rate, self.audio_config.channels)

        self.last_frame_sequence = -1
        self.image_dir = IMAGE_SAVE_DIR
        Path(self.image_dir).mkdir(parents=True, exist_ok=True)

        self.mac_to_websocket = {}
        self.milestone_sessions = {}

        logger.info("✅ ESP WebSocket服务器初始化完成")

    def _create_ssl_context(self):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if not os.path.exists(self.cert_file):
            raise FileNotFoundError(f"证书文件不存在: {self.cert_file}")
        if not os.path.exists(self.key_file):
            raise FileNotFoundError(f"密钥文件不存在: {self.key_file}")
        try:
            ssl_context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            return ssl_context
        except Exception as e:
            raise RuntimeError(f"加载SSL证书失败: {e}")

    async def start(self, dialog_session=None) -> None:
        self.dialog_session = dialog_session
        server_kwargs = {
            'ws_handler': self.handle_connection,
            'host': self.host,
            'port': self.port,
            'ping_interval': None,
            'ping_timeout': None,
        }
        if self.use_ssl:
            ssl_context = self._create_ssl_context()
            server_kwargs['ssl'] = ssl_context
            protocol = "wss"
        else:
            protocol = "ws"
        self.server = await websockets.serve(**server_kwargs)
        logger.info(f"✅ ESP WebSocket服务器已启动: {protocol}://{self.host}:{self.port}")
        try:
            await self.server.wait_closed()
        finally:
            await self.cleanup()

    async def handle_connection(self, websocket: websockets.WebSocketServerProtocol, path: str) -> None:
        self.active_connections.add(websocket)
        client_info = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        logger.info(f"🔌 新ESP设备连接: {client_info}")
        try:
            await websocket.send(json.dumps({
                "status": "connected",
                "message": "成功连接到服务器",
                "timestamp": time.time()
            }))
            async for message in websocket:
                await self.handle_message(websocket, message)
        except websockets.ConnectionClosed as e:
            logger.info(f"🔌 ESP设备断开连接: {client_info}, 原因: {e}")
        except Exception as e:
            logger.error(f"❌ 处理连接错误: {e}")
        finally:
            if hasattr(websocket, 'mac') and websocket.mac in self.mac_to_websocket:
                del self.mac_to_websocket[websocket.mac]
            self.active_connections.discard(websocket)
            self.last_frame_sequence = -1
            mac = getattr(websocket, 'mac', None)
            if mac and mac in self.milestone_sessions:
                del self.milestone_sessions[mac]

    async def handle_message(self, websocket: websockets.WebSocketServerProtocol, message) -> None:
        try:
            if isinstance(message, bytes):
                await self._handle_audio_data(websocket, message)
            else:
                await self._handle_text_message(websocket, message)
        except Exception as e:
            logger.error(f"❌ 处理消息错误: {e}")
            await self._send_error_response(websocket, "处理消息时出错")

    # ==================== 分块发送音频文件 ====================
    async def _send_audio_chunked(self, websocket: websockets.WebSocketServerProtocol, file_path: str,
                                chunk_size: int = 960, delay: float = 0.2) -> bool:
        try:
            start_msg = json.dumps({
                "type": "audio_start",
                "timestamp": time.time(),
                "message": "开始发送音频"
            })
            await websocket.send(start_msg)
            logger.info(f"已发送 audio_start 事件")

            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    await websocket.send(chunk)
                    await asyncio.sleep(delay)

            end_msg = json.dumps({
                "type": "audio_end",
                "timestamp": time.time(),
                "message": "音频发送完成"
            })
            await websocket.send(end_msg)
            logger.info(f"已发送 audio_end 事件")
            return True
        except Exception as e:
            logger.error(f"分块发送音频文件失败 {file_path}: {e}")
            try:
                error_end_msg = json.dumps({
                    "type": "audio_end",
                    "timestamp": time.time(),
                    "message": "音频发送异常终止"
                })
                await websocket.send(error_end_msg)
            except:
                pass
            return False

    # ==================== 辅助：保存 PCM 为 WAV ====================
    def _save_pcm_to_wav(self, pcm_data: bytes, wav_path: Path, sample_rate=16000, channels=1, bits_per_sample=16):
        """将 PCM 数据保存为 WAV 文件"""
        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8
        data_size = len(pcm_data)
        with open(wav_path, 'wb') as f:
            f.write(b'RIFF')
            f.write(struct.pack('<I', 36 + data_size))
            f.write(b'WAVE')
            f.write(b'fmt ')
            f.write(struct.pack('<I', 16))
            f.write(struct.pack('<H', 1))
            f.write(struct.pack('<H', channels))
            f.write(struct.pack('<I', sample_rate))
            f.write(struct.pack('<I', byte_rate))
            f.write(struct.pack('<H', block_align))
            f.write(struct.pack('<H', bits_per_sample))
            f.write(b'data')
            f.write(struct.pack('<I', data_size))
            f.write(pcm_data)

    # ==================== 音频数据处理 ====================
    async def _handle_audio_data(self, websocket: websockets.WebSocketServerProtocol, data: bytes) -> None:
        mac = getattr(websocket, 'mac', None)

        if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
            if mac and mac in self.milestone_sessions:
                sess = self.milestone_sessions[mac]
                if sess.get('waiting_for_photo'):
                    milestone_num = sess['milestone_num']
                    mac_safe = mac.replace(':', '-')
                    save_dir = Path(f"task/milestones{milestone_num}/answer/{mac_safe}")
                    save_dir.mkdir(parents=True, exist_ok=True)
                    save_path = save_dir / "picture.jpg"
                    with open(save_path, 'wb') as f:
                        f.write(data)
                    logger.info(f"📸 里程碑照片已保存: {save_path} ({len(data)} bytes)")
                    # 异步后处理：生成人物画像 + AI图片
                    asyncio.create_task(self._finalize_milestone(mac, milestone_num))
                    del self.milestone_sessions[mac]
                    await websocket.send(json.dumps({"type": "milestone_complete", "milestone": milestone_num}))
                    return

            filename = self._save_jpeg_image(data)
            if filename:
                logger.info(f"📸 收到并保存图片: {filename} ({len(data)} bytes)")
                await websocket.send(json.dumps({
                    "status": "image_received",
                    "filename": filename,
                    "size": len(data),
                    "timestamp": time.time()
                }))
            else:
                logger.error(f"❌ 图片保存失败，大小 {len(data)} 字节")
                await self._send_error_response(websocket, "图片保存失败")
            return

        is_valid, msg, header, payload = self.frame_parser.parse_frame_header(data)
        if not is_valid:
            logger.warning(f"❌ 帧校验失败: {msg}")
            return

        logger.debug(f"✅ 接收音频帧: {msg}")
        if self.last_frame_sequence != -1 and header.sequence != (self.last_frame_sequence + 1) % 65536:
            logger.warning(f"⚠️  帧序号不连续（上一帧{self.last_frame_sequence}，当前帧{header.sequence}）")
        self.last_frame_sequence = header.sequence

        # 里程碑收集（payload 可能是 PCM 或 Opus，统一当作原始音频数据保存）
        if mac and mac in self.milestone_sessions:
            sess = self.milestone_sessions[mac]
            if sess.get('collecting_audio'):
                if len(sess['audio_packets']) < 3:
                    logger.info(f"📦 第 {len(sess['audio_packets'])+1} 个音频包，长度={len(payload)}，前8字节={payload[:8].hex()}")
                sess['audio_packets'].append(payload)
                logger.debug(f"收集音频包: +{len(payload)} 字节，总包数 {len(sess['audio_packets'])}")
                return

        # 对话转发（仅当数据是 Opus 格式且对话未结束时）
        pcm_data = self.opus_decoder.decode(payload, self.audio_config.chunk_size)
        if pcm_data is not None:
            if self.dialog_session and hasattr(self.dialog_session, 'client') and not self.dialog_session.is_session_finished:
                try:
                    await self.dialog_session.client.task_request(pcm_data)
                except Exception as e:
                    logger.error(f"❌ 转发音频数据到对话会话失败: {e}")
        else:
            logger.debug(f"忽略非 Opus 数据（长度 {len(payload)}）")

    async def _finalize_milestone(self, mac: str, milestone_num: int):
        """里程碑完成后：生成劳动评价报告 → 生成 AI 图片 → 推进里程碑进度"""
        try:
            from persona_generator import generate_labor_evaluation_for_milestone
            from AI_toy_picture import generate_milestone_image

            # 1. 生成劳动评价并存入 assessment_evaluation
            await generate_labor_evaluation_for_milestone(mac, milestone_num, self.db_manager)
            logger.info(f"✅ 劳动评价报告已生成 (MAC={mac}, milestone={milestone_num})")

            # 2. 生成 AI 图片（基于刚生成的评价报告）
            await generate_milestone_image(mac, milestone_num, self.db_manager)
            logger.info(f"✅ 里程碑 {milestone_num} AI 图片已生成 (MAC={mac})")

            # 3. 推进里程碑进度
            total_milestones = await self.db_manager.get_total_milestones_for_device(mac)
            next_milestone = milestone_num + 1
            if next_milestone <= total_milestones:
                await self.db_manager.update_device_current_milestone(mac, next_milestone)
                if self.dialog_session and self.dialog_session.mac_address == mac:
                    await self.dialog_session.update_current_milestone(next_milestone)
                logger.info(f"📈 设备 {mac} 里程碑从 {milestone_num} 更新到 {next_milestone}")
            else:
                logger.info(f"🏁 设备 {mac} 已完成所有里程碑 (当前里程碑 {milestone_num})")

        except Exception as e:
            logger.error(f"❌ 里程碑 {milestone_num} 后处理失败: {e}", exc_info=True)

    def _save_jpeg_image(self, data: bytes) -> Optional[str]:
        if not data or len(data) < 2:
            return None
        if data[0] != 0xFF or data[1] != 0xD8:
            logger.warning(f"无效的 JPEG 头: {data[0]:02X}{data[1]:02X}")
            return None
        timestamp = int(time.time() * 1000)
        filename = f"img_{timestamp}.jpg"
        filepath = os.path.join(self.image_dir, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(data)
            return filename
        except Exception as e:
            logger.error(f"保存图片失败 {filepath}: {e}")
            return None

    # ==================== 文本消息处理 ====================
    async def _handle_text_message(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        logger.info(f"📩 收到文本消息: {message}")
        try:
            msg_data = json.loads(message)
            msg_type = msg_data.get('type')

            if msg_type and (msg_type.startswith('milestones_answer_') or
                             msg_type.startswith('answer_question_') or
                             msg_type.startswith('end_answer_question_')):
                mac = getattr(websocket, 'mac', None)
                if not mac:
                    logger.warning("无法获取设备 MAC")
                    await self._send_error_response(websocket, "未提供 MAC 地址")
                    return
                await self._handle_milestone_text(websocket, mac, msg_data)
                return

            if msg_type == 'device_info':
                await self._handle_device_info(websocket, msg_data)
            elif msg_type == 'wakeup':
                await self._handle_wakeup(websocket)
            elif msg_type == 'ping':
                await self._handle_ping(websocket)
            else:
                await self._handle_chat_message(websocket, message)
        except json.JSONDecodeError:
            await self._handle_chat_message(websocket, message)

    async def _handle_milestone_text(self, websocket: websockets.WebSocketServerProtocol, mac: str, msg_data: dict):
        msg_type = msg_data.get('type')
        if not msg_type:
            return

        match = re.match(r'milestones_answer_(\d+)', msg_type)
        if match:
            milestone_num = int(match.group(1))
            logger.info(f"收到里程碑开始指令: 里程碑 {milestone_num}")
            await self._start_milestone_flow(websocket, mac, milestone_num)
            await websocket.send(json.dumps({"type": f"milestones_answer_{milestone_num}", "status": "started"}))
            return

        match = re.match(r'answer_question_(\d+)', msg_type)
        if match:
            question_num = int(match.group(1))
            sess = self.milestone_sessions.get(mac)
            if not sess:
                logger.warning(f"收到 answer_question_{question_num} 但没有活跃会话")
                return
            total_questions = sess.get('total_questions', 0)
            if question_num > total_questions:
                logger.warning(f"收到无效问题序号 {question_num}，最大为 {total_questions}，忽略")
                return
            if sess.get('expecting_answer_for') is None:
                logger.warning(f"收到 answer_question_{question_num} 但当前未期望任何回答，忽略")
                return
            if sess.get('expecting_answer_for') != question_num:
                logger.warning(f"期望回答问题 {sess.get('expecting_answer_for')}，收到 {question_num}，忽略")
                return
            sess['collecting_audio'] = True
            sess['current_question_num'] = question_num
            sess['audio_packets'] = []   # 存储 Opus 帧（已经去掉了自定义帧头）
            logger.info(f"开始收集问题 {question_num} 的音频回答")
            return

        match = re.match(r'end_answer_question_(\d+)', msg_type)
        if match:
            question_num = int(match.group(1))
            sess = self.milestone_sessions.get(mac)
            if not sess or not sess.get('collecting_audio'):
                logger.warning(f"收到 end_answer_question_{question_num} 但未在收集音频")
                return
            if sess.get('current_question_num') != question_num:
                logger.warning(f"当前正在收集问题 {sess.get('current_question_num')}，收到结束序号 {question_num}，忽略")
                return
            sess['collecting_audio'] = False
            opus_packets = sess.get('audio_packets', [])
            milestone_num = sess['milestone_num']
            mac_safe = mac.replace(':', '-')
            save_dir = Path(f"task/milestones{milestone_num}/answer/{mac_safe}")
            save_dir.mkdir(parents=True, exist_ok=True)

            total_frames = len(opus_packets)
            duration_sec = total_frames * OPUS_FRAME_DURATION_MS / 1000.0
            logger.info(f"收集到 {total_frames} 个 Opus 包，预计音频时长 {duration_sec:.2f} 秒")

            if opus_packets:
                # 解码所有 Opus 包为 PCM
                pcm_data = bytearray()
                decode_success_count = 0
                for idx, opus_frame in enumerate(opus_packets):
                    decoded = self.opus_decoder.decode(opus_frame, self.audio_config.chunk_size)
                    if decoded:
                        pcm_data.extend(decoded)
                        decode_success_count += 1
                    else:
                        logger.warning(f"解码第 {idx+1} 个 Opus 包失败")
                logger.info(f"成功解码 {decode_success_count}/{total_frames} 帧，PCM 数据大小 {len(pcm_data)} 字节")

                if pcm_data:
                    # 保存为 WAV 文件
                    wav_path = save_dir / f"answer{question_num}.wav"
                    self._save_pcm_to_wav(pcm_data, wav_path, sample_rate=16000, channels=1, bits_per_sample=16)
                    logger.info(f"PCM 转 WAV 已保存: {wav_path}")
                    # 立即识别音频
                    try:
                        recognized_text = recognize_audio(str(wav_path))
                        logger.info(f"✅ 语音识别结果: {recognized_text}")
                        # 存入数据库
                        device = await self.db_manager.get_device_by_mac(mac)
                        if device:
                            await self.db_manager.execute(
                                """
                                INSERT INTO milestone_answers (device_id, milestone_number, question_index, answer_text, answer_audio_path)
                                VALUES (%s, %s, %s, %s, %s)
                                ON DUPLICATE KEY UPDATE answer_text = VALUES(answer_text)
                                """,
                                (device['id'], milestone_num, question_num, recognized_text, str(wav_path))
                            )
                    except Exception as e:
                        logger.error(f"❌ 语音识别失败: {e}")
                else:
                    logger.error("没有成功解码任何 Opus 包，无法生成 WAV")
            else:
                logger.warning("没有收集到任何音频包")

            sess['audio_packets'] = []
            sess['current_question_num'] = None
            sess['current_q_index'] = question_num + 1
            sess.pop('expecting_answer_for', None)
            await self._send_next_question(mac)
            return

    async def _start_milestone_flow(self, websocket: websockets.WebSocketServerProtocol, mac: str, milestone_num: int):
        question_dir = Path(f"task/milestones{milestone_num}/question")
        if not question_dir.exists():
            logger.error(f"问题目录不存在: {question_dir}")
            return

        opus_files = sorted(question_dir.glob("*.opus"))
        numeric_files = [f for f in opus_files if f.stem.isdigit()]
        take_photo_file = question_dir / "take_photo.opus"

        self.milestone_sessions[mac] = {
            'milestone_num': milestone_num,
            'current_q_index': 1,
            'total_questions': len(numeric_files),
            'collecting_audio': False,
            'audio_packets': [],
            'current_question_num': None,
            'waiting_for_photo': False,
            'photo_sent': False,
            'numeric_questions': numeric_files,
            'take_photo_file': take_photo_file if take_photo_file.exists() else None,
        }
        await self._send_next_question(mac)

    async def _send_next_question(self, mac: str):
        sess = self.milestone_sessions.get(mac)
        if not sess:
            return
        q_index = sess['current_q_index']
        milestone_num = sess['milestone_num']
        numeric_files = sess['numeric_questions']
        take_photo_file = sess.get('take_photo_file')
        websocket = self.mac_to_websocket.get(mac)
        if not websocket:
            logger.error(f"设备 {mac} 未连接")
            return

        if q_index > len(numeric_files):
            if take_photo_file and not sess.get('photo_sent', False):
                file_path = str(take_photo_file)
                logger.info(f"发送拍照指令: {file_path}")
                sess['expecting_answer_for'] = None
                await websocket.send(json.dumps({"type": "answer_question_photo"}))
                logger.info("已发送 answer_question_photo 消息")
                success = await self._send_audio_chunked(websocket, file_path)
                if success:
                    sess['photo_sent'] = True
                    sess['waiting_for_photo'] = True
                    logger.info("拍照指令已发送，等待图片数据")
                else:
                    logger.error("发送拍照指令失败")
                return
            else:
                logger.info(f"里程碑 {milestone_num} 流程全部完成")
                if mac in self.milestone_sessions:
                    del self.milestone_sessions[mac]
                return

        file_path = None
        for f in numeric_files:
            if f.stem == str(q_index):
                file_path = str(f)
                break
        if not file_path:
            logger.error(f"未找到问题 {q_index} 的音频文件")
            return

        logger.info(f"发送问题 {q_index}: {file_path}")
        success = await self._send_audio_chunked(websocket, file_path)
        if success:
            sess['expecting_answer_for'] = q_index
            logger.info(f"问题 {q_index} 已发送，等待 'answer_question_{q_index}'")
        else:
            logger.error(f"发送问题 {q_index} 失败")

    async def _handle_device_info(self, websocket: websockets.WebSocketServerProtocol, msg_data: dict) -> None:
        mac = msg_data.get('mac', 'Unknown')
        device_type = msg_data.get('device_type', 'ESP32')
        firmware_version = msg_data.get('firmware_version', 'Unknown')
        name = msg_data.get('name', f'设备_{mac[-5:]}')
        logger.info(f"📱 设备信息: MAC={mac}, 类型={device_type}, 固件={firmware_version}")
        websocket.mac = mac
        self.mac_to_websocket[mac] = websocket
        if self.dialog_session:
            self.dialog_session.mac_address = mac
        if self.db_manager:
            try:
                device_id = await self.db_manager.upsert_device(mac, name)
                logger.info(f"✅ 设备信息已保存到数据库: {mac}, device_id={device_id}")
            except Exception as e:
                logger.error(f"❌ 保存设备信息失败: {e}")
        await websocket.send(json.dumps({
            "status": "received",
            "message": "设备信息已接收",
            "server_time": time.time()
        }))

    async def _handle_wakeup(self, websocket: websockets.WebSocketServerProtocol) -> None:
        logger.info("🔔 收到唤醒指令，重连大模型...")
        if not self.dialog_session or not hasattr(self.dialog_session, 'client'):
            await self._send_error_response(websocket, "对话会话未初始化")
            return
        try:
            import copy
            self.dialog_session.client.start_session_req = copy.deepcopy(self.dialog_session.custom_start_session_req)
            if hasattr(self.dialog_session.client, 'close'):
                await self.dialog_session.client.close()
                await asyncio.sleep(0.1)
            await self.dialog_session.client.connect()
            if hasattr(self.dialog_session, 'restart_receive_loop'):
                await self.dialog_session.restart_receive_loop()
            else:
                await self.dialog_session.client.say_hello()
                if hasattr(self.dialog_session, '_receive_loop_task') and not self.dialog_session._receive_loop_task.done():
                    self.dialog_session._receive_loop_task.cancel()
                self.dialog_session._receive_loop_task = asyncio.create_task(self.dialog_session.receive_loop())
            self.dialog_session.is_session_finished = False
            self.dialog_session.is_user_querying = False
            self.dialog_session.is_sending_chat_tts_text = False
            logger.info("✅ 大模型重连成功")
            await websocket.send(json.dumps({"status": "success", "message": "大模型重连成功"}))
        except Exception as e:
            logger.error(f"❌ 大模型重连失败: {e}")
            await self._send_error_response(websocket, "大模型重连失败")

    async def _handle_ping(self, websocket: websockets.WebSocketServerProtocol) -> None:
        await websocket.send(json.dumps({"type": "pong", "timestamp": time.time()}))

    async def _handle_chat_message(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        if self.dialog_session and hasattr(self.dialog_session, 'client') and hasattr(self.dialog_session.client, 'chat_text_query'):
            try:
                await self.dialog_session.client.chat_text_query(message)
                await websocket.send(json.dumps({"status": "received", "message": "消息已转发"}))
            except Exception as e:
                logger.error(f"❌ 转发聊天消息失败: {e}")
                await self._send_error_response(websocket, "转发消息失败")
        else:
            await self._send_error_response(websocket, "对话会话未就绪")

    async def _send_error_response(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        try:
            await websocket.send(json.dumps({
                "status": "error",
                "message": message,
                "timestamp": time.time()
            }))
        except Exception as e:
            logger.error(f"❌ 发送错误响应失败: {e}")

    async def broadcast_message(self, message: str) -> None:
        if not self.active_connections:
            logger.warning("⚠️  没有活跃连接，无法广播消息")
            return
        logger.info(f"📢 广播消息到{len(self.active_connections)}个客户端")
        tasks = [self._safe_send(conn, message) for conn in self.active_connections.copy()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_send(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        try:
            await websocket.send(message)
        except websockets.ConnectionClosed:
            self.active_connections.discard(websocket)
        except Exception as e:
            logger.error(f"❌ 发送消息失败: {e}")
            self.active_connections.discard(websocket)

    async def stop(self) -> None:
        if self.server:
            logger.info("🔴 关闭服务器...")
            self.server.close()
            await self.server.wait_closed()
        await self.cleanup()

    async def cleanup(self) -> None:
        if self.active_connections:
            close_tasks = [self._safe_close_connection(conn) for conn in self.active_connections.copy()]
            await asyncio.gather(*close_tasks, return_exceptions=True)

    async def _safe_close_connection(self, websocket: websockets.WebSocketServerProtocol) -> None:
        try:
            await websocket.close()
        except Exception as e:
            logger.error(f"❌ 关闭连接失败: {e}")

    def get_server_status(self) -> dict:
        return {
            "server_running": self.server is not None,
            "active_connections": len(self.active_connections),
            "last_frame_sequence": self.last_frame_sequence,
            "opus_consecutive_errors": self.opus_decoder.consecutive_error_count,
            "image_save_dir": str(self.image_dir)
        }