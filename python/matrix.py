# UNO Q Internet Radio v3 - 13x8 LED matrix frames (unchanged from v1)
# SPDX-License-Identifier: MIT

from player import IDLE, CONNECTING, PLAYING, ERROR

# --------------------------------------------------------------------------
COLS, ROWS = 13, 8

DIGITS = {  # 3x5 font, each row is 3 bits (MSB = left column)
    "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7],
    "3": [7, 1, 7, 1, 7], "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7],
    "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2], "8": [7, 5, 7, 5, 7],
    "9": [7, 5, 7, 1, 7],
}
ICONS = {  # 4x5 icons drawn at columns 9-12
    PLAYING: [8, 12, 14, 12, 8],      # play triangle
    IDLE:    [10, 10, 10, 10, 10],    # pause bars
    ERROR:   [9, 6, 6, 6, 9],         # X
}


def render(station_idx, state, volume, muted, tick):
    grid = [[0] * COLS for _ in range(ROWS)]

    def put(x, y):
        if 0 <= x < COLS and 0 <= y < ROWS:
            grid[y][x] = 1

    # Station number 01..99 at rows 1-5, columns 0-2 and 4-6
    label = f"{(station_idx + 1) % 100:02d}"
    for d, ch in enumerate(label):
        for r, bits in enumerate(DIGITS[ch]):
            for c in range(3):
                if bits & (4 >> c):
                    put(d * 4 + c, r + 1)

    # State icon at columns 9-12
    if state == CONNECTING:
        for i in range(tick % 4 + 1):            # growing progress dots
            put(9 + i, 3)
    else:
        for r, bits in enumerate(ICONS.get(state, ICONS[IDLE])):
            for c in range(4):
                if bits & (8 >> c):
                    put(9 + c, r + 1)

    # Volume bar on the bottom row (blinks when muted)
    if not (muted and tick % 2):
        lit = round(volume / 100 * COLS)
        for x in range(lit):
            put(x, 7)

    # Pack row-major, MSB-first into four 32-bit words (104 bits + padding)
    bits = [grid[y][x] for y in range(ROWS) for x in range(COLS)] + [0] * 24
    words = []
    for w in range(4):
        v = 0
        for b in bits[w * 32:(w + 1) * 32]:
            v = (v << 1) | b
        words.append(v)
    return "".join(f"{w:08x}" for w in words)


