import asyncio
import websockets
import ssl
import os
import sys
import pyaudio
import json
import time
from queue import Queue, Full, Empty
import threading
from typing import Dict, Any, Set, Optional
import ctypes
import struct
import logging
from dataclasses import dataclass
import base64
import re
from pathlib import Path

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
        opus_data = data[6:6+data_size]
        header = FrameHeader(magic1, magic2, sequence, data_size)
        return True, f"帧{sequence}校验通过（Opus数据{data_size}字节）", header, opus_data

class AudioPlayer:
    def __init__(self, config: AudioConfig):
        self.config = config
        self.pyaudio = pyaudio.PyAudio()
        self.audio_stream = None
        self.pcm_queue = Queue(maxsize=config.buffer_size)
        self.is_playing = False
        self.play_thread = None
        self._init_audio_stream()
    
    def _init_audio_stream(self):
        try:
            self.audio_stream = self.pyaudio.open(
                format=self.config.format,
                channels=self.config.channels,
                rate=self.config.sample_rate,
                output=True,
                frames_per_buffer=self.config.chunk_size
            )
            logger.info(f"✅ 音频输出流已打开: {self.config.sample_rate}Hz, {self.config.channels}声道")
        except Exception as e:
            logger.error(f"❌ 打开音频输出流失败: {e}")
            self.audio_stream = None
    
    def start_playing(self):
        if not self.is_playing and self.audio_stream:
            self.is_playing = True
            self.play_thread = threading.Thread(target=self._play_loop, daemon=True)
            self.play_thread.start()
            logger.info("✅ 音频播放线程已启动")
    
    def stop_playing(self):
        self.is_playing = False
        if self.play_thread and self.play_thread.is_alive():
            self.play_thread.join(timeout=2.0)
            logger.info("🔴 播放线程已停止")
    
    def add_pcm_data(self, pcm_data: bytes) -> bool:
        try:
            queue_size = self.pcm_queue.qsize()
            if queue_size >= self.config.queue_warn_threshold:
                logger.warning(f"⚠️  队列即将满（当前{queue_size}/{self.config.buffer_size}帧）")
            try:
                self.pcm_queue.put(pcm_data, block=False)
                return True
            except Full:
                try:
                    self.pcm_queue.put(pcm_data, block=True, timeout=0.01)
                    logger.warning("⚠️  队列满，等待10ms后入队成功")
                    return True
                except Full:
                    try:
                        old_data = self.pcm_queue.get_nowait()
                        self.pcm_queue.put(pcm_data, block=False)
                        logger.warning(f"⚠️  缓冲区满，丢弃最旧帧（{len(old_data)}字节）")
                        return True
                    except:
                        logger.error("❌ 队列操作失败")
                        return False
        except Exception as e:
            logger.error(f"❌ 添加PCM数据失败: {e}")
            return False
    
    def _play_loop(self):
        buffered_frames = 0
        frame_duration = 0.06
        while self.is_playing:
            try:
                if buffered_frames < self.config.min_buffer_frames:
                    try:
                        pcm_data = self.pcm_queue.get(timeout=0.05)
                        buffered_frames += 1
                        continue
                    except Empty:
                        time.sleep(0.001)
                        continue
                try:
                    pcm_data = self.pcm_queue.get(timeout=0.02)
                    buffered_frames = max(0, buffered_frames - 1)
                except Empty:
                    time.sleep(0.001)
                    continue
                if self.audio_stream and self.audio_stream.is_active():
                    try:
                        start_time = time.time()
                        expected_length = self.config.chunk_size * self.config.channels * 2
                        if len(pcm_data) != expected_length:
                            logger.error(f"❌ PCM数据长度错误: 期望{expected_length}字节，实际{len(pcm_data)}字节")
                            continue
                        self.audio_stream.write(pcm_data)
                        elapsed = time.time() - start_time
                        sleep_time = max(0, frame_duration - elapsed)
                        if sleep_time > 0:
                            time.sleep(sleep_time)
                        logger.debug(f"✅ 播放PCM帧: {len(pcm_data)}字节，耗时{elapsed*1000:.2f}ms，等待{sleep_time*1000:.2f}ms，队列剩余{self.pcm_queue.qsize()}帧")
                    except Exception as e:
                        logger.error(f"❌ 播放音频失败: {e}")
            except Exception as e:
                logger.error(f"❌ 播放线程错误: {e}")
                time.sleep(0.1)
    
    def cleanup(self):
        self.stop_playing()
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except Exception as e:
                logger.error(f"❌ 关闭音频流错误: {e}")
        if self.pyaudio:
            self.pyaudio.terminate()
            logger.info("🔴 PyAudio已终止")

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
        
        # MAC 映射
        self.mac_to_websocket = {}
        
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
            'port': self.port
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

    async def handle_message(self, websocket: websockets.WebSocketServerProtocol, message) -> None:
        try:
            if isinstance(message, bytes):
                await self._handle_audio_data(websocket, message)
            else:
                await self._handle_text_message(websocket, message)
        except Exception as e:
            logger.error(f"❌ 处理消息错误: {e}")
            await self._send_error_response(websocket, "处理消息时出错")

    async def _handle_image_data(self, websocket: websockets.WebSocketServerProtocol, data: bytes) -> None:
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

    async def _handle_audio_data(self, websocket: websockets.WebSocketServerProtocol, data: bytes) -> None:
        if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
            logger.debug("检测到 JPEG 二进制数据，转图片处理")
            await self._handle_image_data(websocket, data)
            return
        
        is_valid, msg, header, opus_data = self.frame_parser.parse_frame_header(data)
        if not is_valid:
            logger.warning(f"❌ 帧校验失败: {msg}")
            return
        
        logger.debug(f"✅ 接收音频帧: {msg}，原始数据总长度{len(data)}字节")
        if self.last_frame_sequence != -1 and header.sequence != (self.last_frame_sequence + 1) % 65536:
            logger.warning(f"⚠️  帧序号不连续（上一帧{self.last_frame_sequence}，当前帧{header.sequence}）")
        self.last_frame_sequence = header.sequence
        
        pcm_data = self.opus_decoder.decode(opus_data, self.audio_config.chunk_size)
        if pcm_data is None:
            return
            
        if self.dialog_session and hasattr(self.dialog_session, 'client'):
            try:
                await self.dialog_session.client.task_request(pcm_data)
            except Exception as e:
                logger.error(f"❌ 转发音频数据到对话会话失败: {e}")

    async def _handle_text_message(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        logger.info(f"📩 收到文本消息: {message}")
        try:
            msg_data = json.loads(message)
            msg_type = msg_data.get('type')
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
        if hasattr(self, 'db_manager') and self.db_manager:
            try:
                await self.db_manager.upsert_device(mac, name)
            except Exception as e:
                logger.error(f"保存设备信息到数据库失败: {e}")
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
            mac = self.dialog_session.mac_address
            if mac and hasattr(self, 'db_manager') and self.db_manager:
                summary = await self.db_manager.get_latest_summary_by_mac(mac)
                if summary:
                    original_role = self.dialog_session.custom_start_session_req["dialog"]["system_role"]
                    enhanced_role = f"{original_role}\n\n【历史对话记忆】\n{summary}\n\n请根据以上历史记忆继续与小朋友自然对话。"
                    self.dialog_session.custom_start_session_req["dialog"]["system_role"] = enhanced_role
                    logger.info(f"✅ 已刷新历史摘要，长度 {len(summary)} 字符")
                else:
                    logger.info("ℹ️ 未找到历史摘要，将作为新对话开始")
            else:
                logger.warning("⚠️ 无法获取 MAC 地址或数据库管理器，跳过摘要加载")
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
            logger.info("✅ 大模型重连成功（已加载最新记忆）")
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

    async def send_emergency_audio(self, mac: str, ogg_opus_data: bytes) -> bool:
        """发送紧急音频（OGG Opus 文件，带开始/结束通知）"""
        ws = self.mac_to_websocket.get(mac)
        if not ws:
            logger.error(f"紧急音频发送失败：设备 {mac} 未连接")
            return False
        
        # 发送开始通知
        start_notification = {
            "type": "urgent_audio_start",
            "timestamp": int(time.time()),
            "message": "开始发送紧急音频"
        }
        try:
            await ws.send(json.dumps(start_notification))
            logger.info(f"已发送紧急音频开始通知到设备 {mac}")
        except Exception as e:
            logger.error(f"发送紧急音频开始通知失败: {e}")
            return False
        
        # 进入紧急模式
        if self.dialog_session:
            self.dialog_session.emergency_mode = True
            self.dialog_session.accumulated_buffer = b''
            self.dialog_session.current_audio_buffer = b''
        
        try:
            # 直接发送 OGG Opus 数据
            await ws.send(ogg_opus_data)
            logger.info(f"紧急音频已发送给 {mac}，大小 {len(ogg_opus_data)} 字节")
            return True
        except Exception as e:
            logger.error(f"发送紧急音频异常: {e}")
            return False
        finally:
            # 发送结束通知
            end_notification = {
                "type": "urgent_audio_end",
                "timestamp": int(time.time()),
                "message": "紧急音频发送完成"
            }
            try:
                await ws.send(json.dumps(end_notification))
                logger.info(f"已发送紧急音频结束通知到设备 {mac}")
            except Exception as e:
                logger.error(f"发送紧急音频结束通知失败: {e}")
            # 退出紧急模式
            if self.dialog_session:
                self.dialog_session.emergency_mode = False

    async def broadcast_message(self, message: str) -> None:
        if not self.active_connections:
            logger.warning("⚠️  没有活跃连接，无法广播消息")
            return
        logger.info(f"📢 广播消息到{len(self.active_connections)}个客户端")
        tasks = []
        for conn in self.active_connections.copy():
            tasks.append(self._safe_send(conn, message))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed_count = sum(1 for r in results if isinstance(r, Exception))
        if failed_count:
            logger.warning(f"⚠️  {failed_count}个客户端消息发送失败")

    async def _safe_send(self, websocket: websockets.WebSocketServerProtocol, message: str) -> None:
        try:
            await websocket.send(message)
        except websockets.ConnectionClosed:
            self.active_connections.discard(websocket)
        except Exception as e:
            logger.error(f"❌ 发送消息到 {websocket.remote_address} 失败: {e}")
            self.active_connections.discard(websocket)

    async def stop(self) -> None:
        if self.server:
            logger.info("🔴 关闭服务器...")
            self.server.close()
            await self.server.wait_closed()
            logger.info("🔴 服务器已关闭")
        await self.cleanup()

    async def cleanup(self) -> None:
        logger.info("🔴 清理服务器资源...")
        if self.active_connections:
            close_tasks = []
            for conn in self.active_connections.copy():
                close_tasks.append(self._safe_close_connection(conn))
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
            "opus_consecutive_errors": self.opus_decoder.consecutive_error_count if self.opus_decoder else 0,
            "image_save_dir": str(self.image_dir)
        }