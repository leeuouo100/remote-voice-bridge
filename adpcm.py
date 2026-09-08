"""
IMA-ADPCM decoder — standalone port of vRemoter's ADPCMDecoder.swift.
"""

_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2494, 2740, 3008, 3307, 3638, 4002, 4402, 4842, 5327,
    5860, 6446, 7091, 7800, 8580, 9438, 10382, 11420, 12562, 13818,
    15200, 16720, 18392, 20231, 22254, 24479, 26927, 29620, 32767,
)

_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8)


class IMAADPCMDecoder:
    def __init__(self):
        self.reset()

    def reset(self, predictor: int = 0, step_index: int = 0) -> None:
        self.predictor = predictor
        self.step_index = step_index

    def decode(self, data: bytes) -> list[int]:
        samples = []
        for byte in data:
            samples.append(self._nibble(byte >> 4))
            samples.append(self._nibble(byte & 0x0F))
        return samples

    def _nibble(self, nibble: int) -> int:
        step = _STEP_TABLE[self.step_index]
        diff = step >> 3
        if nibble & 1: diff += step >> 2
        if nibble & 2: diff += step >> 1
        if nibble & 4: diff += step
        if nibble & 8:
            self.predictor -= diff
        else:
            self.predictor += diff
        self.predictor = min(32767, max(-32768, self.predictor))
        self.step_index += _INDEX_TABLE[nibble & 7]
        self.step_index = min(88, max(0, self.step_index))
        return self.predictor
