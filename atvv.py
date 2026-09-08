"""
ATVV protocol stack — complete port of vRemoter's ATVVProtocol.swift.
Supports v0.4 (per-frame sync) and v1.0 (continuous + AUDIO_SYNC).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from adpcm import IMAADPCMDecoder


# ── UUIDs ─────────────────────────────────────────────────────────────────────
SERVICE_UUID = "AB5E0001-5A21-4F05-BC7D-AF01F617B664"
TX_UUID      = "AB5E0002-5A21-4F05-BC7D-AF01F617B664"
AUDIO_UUID   = "AB5E0003-5A21-4F05-BC7D-AF01F617B664"
CTL_UUID     = "AB5E0004-5A21-4F05-BC7D-AF01F617B664"

GET_CAPS_CMD   = bytes([0x0A, 0x01, 0x00, 0x00, 0x03, 0x03])

# ── Control opcodes (CTL channel, remote → host) ─────────────────────────
# 方向复用：同一 opcode 在 TX(host→remote) 与 CTL(remote→host) 上含义不同。
#   TX  0x0A = GET_CAPS      CTL 0x0A = AUDIO_SYNC (v1.0)
#   TX  0x0C = MIC_OPEN      CTL 0x0C = MIC_OPEN_RESULT / error
_OP_CAPS         = 0x0B
_OP_AUDIO_START  = 0x04
_OP_AUDIO_STOP   = 0x00
_OP_MIC_OPEN_R   = 0x0C
_OP_START_SEARCH = 0x08   # 语音键按下 → host 必须回应 MIC_OPEN，否则无音频流
_OP_AUDIO_SYNC   = 0x0A   # v1.0 解码同步（codec/seq/predictor/step_index）


# ── Types ─────────────────────────────────────────────────────────────────────
@dataclass
class ATVVCapabilities:
    version: tuple[int, int]   # (1, 0) or (0, 4)
    codecs: int
    interaction_model: int
    frame_size: int

    @property
    def sample_rate(self) -> int:
        return 16000 if (self.codecs & 0x02) else 8000

    def selected_codec(self) -> Optional[int]:
        if self.codecs & 0x02: return 0x02
        if self.codecs & 0x01: return 0x01
        return None


@dataclass
class AudioSync:
    sequence: int
    predictor: int
    step_index: int


@dataclass
class ATVVState:
    caps: Optional[ATVVCapabilities] = None
    codec: Optional[int] = None
    sample_rate: int = 16000
    decoder: IMAADPCMDecoder = None  # set in __post_init__
    v10_seq: int = 0
    pending_sync: Optional[AudioSync] = None
    stream_active: bool = False

    def __post_init__(self):
        if self.decoder is None:
            self.decoder = IMAADPCMDecoder()


# ── Protocol ──────────────────────────────────────────────────────────────────
class ATVVProtocol:
    def __init__(self):
        self.state = ATVVState()

    # ── Capabilities parsing ───────────────────────────────────────────────
    @staticmethod
    def parse_caps(data: bytes) -> Optional[ATVVCapabilities]:
        if len(data) < 3 or data[0] != _OP_CAPS:
            return None
        ver = (data[1], data[2])
        if ver == (0, 4):
            if len(data) < 9: return None
            return ATVVCapabilities(
                version=ver, codecs=data[4],
                interaction_model=0,
                frame_size=int.from_bytes(data[5:7], 'big'),
            )
        elif ver == (1, 0):
            if len(data) < 7: return None
            return ATVVCapabilities(
                version=ver, codecs=data[3],
                interaction_model=data[4],
                frame_size=int.from_bytes(data[5:7], 'big'),
            )
        return None

    def accept_caps(self, caps: ATVVCapabilities) -> None:
        selected = caps.selected_codec()
        if selected is None:
            raise RuntimeError("Remote does not support ADPCM 8k/16k")
        self.state.caps = caps
        self.state.codec = selected
        self.state.sample_rate = caps.sample_rate
        self.state.decoder.reset()
        self.state.v10_seq = 0
        self.state.stream_active = False
        self.state.pending_sync = None

    # ── Control event parsing ──────────────────────────────────────────────
    def parse_control(self, data: bytes) -> dict | None:
        if not data:
            return None
        op = data[0]

        if op == _OP_CAPS:
            caps = self.parse_caps(data)
            if caps:
                self.accept_caps(caps)
            return {'type': 'capabilities'} if caps else None

        if op == _OP_AUDIO_START:
            codec  = data[2] if len(data) > 2 else (self.state.codec or 0x02)
            stream = data[3] if len(data) > 3 else 0
            self.state.stream_active = True
            if self.state.pending_sync:
                self._apply_sync(self.state.pending_sync)
                self.state.pending_sync = None
            return {'type': 'audio_start', 'codec': codec, 'stream_id': stream}

        if op == _OP_AUDIO_STOP:
            reason = data[1] if len(data) > 1 else 0
            self.state.stream_active = False
            return {'type': 'audio_stop', 'reason': reason}

        if op == _OP_MIC_OPEN_R:
            code = ((data[1] << 8) | data[2]) if len(data) >= 3 else 0xFFFF
            return {'type': 'mic_open_result', 'code': code}

        if op == _OP_START_SEARCH:
            # 遥控器语音键按下。host 必须回应 MIC_OPEN，否则遥控器不会推音频流。
            # 对标 vRemoter: BLEBridge.handleControl → case .startSearch → openMicrophone()
            return {'type': 'start_search'}

        if op == _OP_AUDIO_SYNC and self.state.caps and self.state.caps.version == (1, 0):
            if len(data) >= 7:
                self.state.pending_sync = AudioSync(
                    sequence  = (data[2] << 8) | data[3],
                    predictor = int.from_bytes(data[4:6], 'big', signed=True),
                    step_index= data[6],
                )
            return {'type': 'audio_sync'}

        return None

    # ── Audio decoding ─────────────────────────────────────────────────────
    def decode_audio(self, data: bytes) -> list[int] | None:
        caps = self.state.caps
        if not caps or not self.state.codec:
            return None
        if not self.state.stream_active:
            return None

        if caps.version == (0, 4):
            # Per-frame sync: [seq_hi, seq_lo, ?, pred_hi, pred_lo, step_idx, ...adpcm...]
            if len(data) < 6 or len(data) != caps.frame_size:
                return None
            pred = int.from_bytes(data[3:5], 'big', signed=True)
            self.state.decoder.reset(predictor=pred, step_index=data[5])
            return self.state.decoder.decode(data[6:])

        else:  # v1.0
            # Apply pending sync first
            if self.state.pending_sync:
                self._apply_sync(self.state.pending_sync)
                self.state.pending_sync = None
            self.state.v10_seq += 1
            return self.state.decoder.decode(data)

    def _apply_sync(self, sync: AudioSync) -> None:
        self.state.decoder.reset(predictor=sync.predictor, step_index=sync.step_index)

    # ── Commands ───────────────────────────────────────────────────────────
    def mic_open_cmd(self) -> bytes:
        if not self.state.caps:
            raise RuntimeError("No capabilities negotiated")
        if self.state.caps.version == (0, 4):
            codec = self.state.codec or 0x01
            return bytes([0x0C, 0x00, codec])
        return bytes([0x0C, 0x00])  # v1.0

    def mic_close_cmd(self, stream_id: int = 0) -> bytes:
        if not self.state.caps:
            raise RuntimeError("No capabilities negotiated")
        if self.state.caps.version == (0, 4):
            return bytes([0x0D])
        return bytes([0x0D, stream_id])

    def keepalive_cmd(self, stream_id: int = 0) -> bytes:
        if not self.state.caps:
            raise RuntimeError("No capabilities negotiated")
        if self.state.caps.version == (0, 4):
            return self.mic_open_cmd()
        return bytes([0x0E, stream_id])
