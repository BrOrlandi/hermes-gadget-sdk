#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace hg {

// One note of a short local sound: a sine at `hz` for `ms`, then `gap_ms` of silence.
// `level` is the peak sample (the timer alarm's beeps use 9000 of 32767).
struct Tone {
  uint16_t hz;
  uint16_t ms;
  uint16_t gap_ms;
  uint16_t level;
};

// The notes as 16-bit mono PCM at `rate`, each with 5 ms ramps so it doesn't click.
std::vector<int16_t> synth_tones(const Tone* tones, size_t count, uint32_t rate);

}  // namespace hg
