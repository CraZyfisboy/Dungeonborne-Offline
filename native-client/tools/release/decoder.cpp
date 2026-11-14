// SPDX-License-Identifier: GPL-3.0-or-later
// A file interface to the separately shipped gooz/ooz decompressor.
#include "ooz.h"

#include <cstdio>
#include <vector>

int main() {
    FILE* input = std::fopen("block.input", "rb");
    FILE* output = std::fopen("block.output", "wb");
    if (!input || !output) return 1;

    uint32_t sizes[2];
    while (std::fread(sizes, sizeof(sizes), 1, input) == 1) {
        const size_t packed_size = sizes[0];
        const size_t raw_size = sizes[1];
        if (packed_size == 0 || packed_size > 2 * 1024 * 1024 ||
            raw_size == 0 || raw_size > 1024 * 1024) return 2;
        std::vector<uint8_t> packed(packed_size + OOZ_SAFE_SPACE);
        std::vector<uint8_t> raw(raw_size + OOZ_SAFE_SPACE);
        if (std::fread(packed.data(), 1, packed_size, input) != packed_size) return 3;
        if (Kraken_Decompress(packed.data(), packed_size, raw.data(), raw_size) !=
            static_cast<int>(raw_size)) return 4;
        if (std::fwrite(raw.data(), 1, raw_size, output) != raw_size) return 5;
    }
    const bool success = std::feof(input) && std::fclose(output) == 0;
    std::fclose(input);
    return success ? 0 : 6;
}
