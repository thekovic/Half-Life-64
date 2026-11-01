"""Run focused control regressions on the host: python test/test_controls.py.

Compile the actual input, binding and selection functions with small hardware
stand-ins, so these checks do not require an emulator or game assets.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    name_at = source.index(f"{name}(")
    start = source.rfind("\n", 0, name_at) + 1
    end = source.index("\n}", name_at) + 2
    return source[start:end]


def main():
    compiler = shutil.which("gcc")
    if compiler is None:
        raise SystemExit("gcc is required to run the control regressions")

    menu = (ROOT / "src/menu.c").read_text()
    weapons = (ROOT / "src/weapons.c").read_text()
    main_source = (ROOT / "src/main.c").read_text()

    bindings = menu[menu.index("#define DEFAULT_BINDS"):menu.index("\n#define SW")]
    definitions = weapons[weapons.index("enum { AM_9MM"):weapons.index("// ------------------------------------------------------------------ state")]
    input_start = main_source.index("    // Mode shift replaces fire")
    input_end = main_source.index("    // driving a train", input_start)
    input_function = (
        "static void apply_buttons(joypad_inputs_t in, joypad_buttons_t pressed) {\n"
        "    const float dt = 1.0f / 60.0f;\n"
        "    input.buttons = 0;\n"
        + main_source[input_start:input_end]
        + "    requested_noclip = cheat_noclip;\n"
        + "    requested_give_all = cheat_give_all;\n"
        + "\n}\n"
    )

    hardware_header = r"""
#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    bool a, b, z, l, r, d_up, d_down, d_left, d_right;
    bool c_up, c_down, c_left, c_right, x, y, start;
} joypad_buttons_t;
typedef struct {
    int stick_x, stick_y;
    joypad_buttons_t btn;
} joypad_inputs_t;
"""

    stand_ins = r"""
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "menu.h"
#include "weapons.h"

static unsigned char saved_settings[64];
static int writes, flashlight_toggles;
static bool has_sram = true;
static void sram_init(void) {}
static int sram_detect(void) { return has_sram; }
static void sram_read(void *dst, int offset, size_t size) {
    (void)offset;
    memcpy(dst, saved_settings, size);
}
static void sram_write(const void *src, int offset, size_t size) {
    (void)offset;
    memcpy(saved_settings, src, size);
    writes++;
}
bool game_pak_ready(void) { return false; }

static int cur, pending, select_slot;
static float select_show;
static struct { uint32_t weapons; } player;
#define CHAN_ITEM 0
#define ATTN_NORM 0
#define PITCH_NORM 0
#define snd_emit(...) ((void)0)

static struct { int buttons; } input;
enum { IN_JUMP = 1, IN_DUCK = 2, IN_USE = 4 };
enum { JOYPAD_PORT_1, JOYPAD_STYLE_GCN };
static bool weapon_fire, weapon_fire2, weapon_reload;
static bool noclip;
static bool requested_noclip, requested_give_all;
static weapon_selection_t weapon_select;
static int pm_get_groundent(void) { return 0; }
static int pm_get_waterlevel(void) { return 0; }
static int joypad_get_style(int port) { (void)port; return -1; }
static void player_toggle_flashlight(void) { flashlight_toggles++; }
"""

    checks = r"""
static void assert_valid_bindings(void)
{
    for (int i = 0; i < NUM_BINDS; i++) {
        for (int j = i + 1; j < NUM_BINDS; j++) {
            if (settings.bind[i] != settings.bind[j]) continue;
            assert(!bind_always_active(i) && !bind_always_active(j));
            assert((i >= BIND_ALTFIRE) != (j >= BIND_ALTFIRE));
        }
    }
}

static void test_bindings(void)
{
    assert(settings.control_scheme == 1);
    for (int action = 0; action < NUM_BINDS; action++) {
        for (int button = 0; button < NUM_PADS; button++) {
            settings_default_binds();
            int previous = settings.bind[action];
            settings_remap_bind(action, button);
            assert(settings.bind[action] == (pad_remappable(button) ? button : previous));
            assert_valid_bindings();
        }
    }

    // Repeated edits must also preserve the separation of both layers.
    for (int i = 0; i < 1000; i++) {
        settings_remap_bind(i % NUM_BINDS, (i * 7) % NUM_PADS);
        assert_valid_bindings();
    }

    for (int scheme = 0; scheme < NUM_SCHEMES; scheme++) {
        settings.control_scheme = scheme;
        settings_default_binds();
        settings_validate_binds();
        int up = scheme == 3 ? PAD_CU : scheme == 4 ? PAD_SU : PAD_DU;
        assert(settings_weapon_pad() == up);
        for (int action = 0; action < NUM_BINDS; action++) {
            assert(pad_remappable(settings.bind[action]));
        }
        assert_valid_bindings();
    }
    settings.control_scheme = 1;
    settings_default_binds();
}

static void test_input(void)
{
    joypad_inputs_t in = {.btn = {.b = true, .z = true, .r = true}};
    apply_buttons(in, in.btn);
    assert(weapon_fire && !weapon_fire2 && !weapon_reload);
    assert(input.buttons == (IN_JUMP | IN_USE));
    assert(flashlight_toggles == 0);

    in.btn.a = true;
    in.btn.l = true;
    apply_buttons(in, in.btn);
    assert(!weapon_fire && weapon_fire2 && weapon_reload);
    assert(input.buttons == IN_DUCK);
    assert(flashlight_toggles == 1);

    // One-shot actions do not repeat while their buttons stay held.
    apply_buttons(in, (joypad_buttons_t){0});
    assert(!weapon_reload && flashlight_toggles == 1);
    assert(weapon_fire2);

    joypad_inputs_t directions[3][4] = {
        {{.btn.d_left = true}, {.btn.d_right = true}, {.btn.d_up = true}, {.btn.d_down = true}},
        {{.btn.c_left = true}, {.btn.c_right = true}, {.btn.c_up = true}, {.btn.c_down = true}},
        {{.stick_x = -64}, {.stick_x = 64}, {.stick_y = 64}, {.stick_y = -64}}
    };
    weapon_selection_t selections[] = {
        WEAPON_SELECT_PREV_SLOT, WEAPON_SELECT_NEXT_SLOT,
        WEAPON_SELECT_PREV_POS, WEAPON_SELECT_NEXT_POS
    };
    for (int scheme = 0; scheme < NUM_SCHEMES; scheme++) {
        settings.control_scheme = scheme;
        int weapon_group = scheme == 3 ? 1 : scheme == 4 ? 2 : 0;
        for (int shift = 0; shift < 2; shift++) {
            for (int group = 0; group < 3; group++) {
                for (int i = 0; i < 4; i++) {
                    pad_stick_update(&(joypad_inputs_t){0});
                    in = directions[group][i];
                    in.btn.a = shift;
                    apply_buttons(in, in.btn);
                    assert(weapon_select == (group == weapon_group ? selections[i] : WEAPON_SELECT_NONE));
                }
            }
        }
    }
    settings.control_scheme = 1;

    settings_remap_bind(BIND_MODE_SHIFT, PAD_B);
    in = (joypad_inputs_t){.btn = {.b = true, .a = true}};
    apply_buttons(in, in.btn);
    assert(weapon_reload && !(input.buttons & IN_USE));
    settings_default_binds();
}

static void test_cheat_shortcuts(void)
{
    joypad_inputs_t directions[3][2] = {
        {{.btn.d_up = true}, {.btn.d_down = true}},
        {{.btn.c_up = true}, {.btn.c_down = true}},
        {{.stick_y = 64}, {.stick_y = -64}}
    };

    for (int scheme = 0; scheme < NUM_SCHEMES; scheme++) {
        settings.control_scheme = scheme;
        int weapon_group = scheme == 3 ? 1 : scheme == 4 ? 2 : 0;
        for (int enabled = 0; enabled < 2; enabled++) {
            settings.cheats = enabled;
            for (int group = 0; group < 3; group++) {
                for (int direction = 0; direction < 2; direction++) {
                    for (int chord = 0; chord < 4; chord++) {
                        pad_stick_update(&(joypad_inputs_t){0});
                        joypad_inputs_t in = directions[group][direction];
                        in.btn.r = (chord & 1) != 0;
                        in.btn.l = (chord & 2) != 0;
                        apply_buttons(in, in.btn);

                        bool expected = enabled && group == weapon_group && chord == 3;
                        assert(requested_noclip == (expected && direction == 0));
                        assert(requested_give_all == (expected && direction == 1));
                        if (expected) assert(weapon_select == WEAPON_SELECT_NONE);
                    }
                }
            }
        }
    }

    // Shifted flashlight/reload no longer trigger cheats with jump and duck held.
    settings.control_scheme = 1;
    joypad_inputs_t shifted = {.btn = {.a = true, .r = true, .l = true, .b = true}};
    int previous_toggles = flashlight_toggles;
    apply_buttons(shifted, shifted.btn);
    assert(!requested_noclip && !requested_give_all);
    assert(weapon_reload && flashlight_toggles == previous_toggles + 1);

    // The chord follows remapped jump and duck actions.
    settings_remap_bind(BIND_JUMP, PAD_B);
    settings_remap_bind(BIND_DUCK, PAD_Z);
    joypad_inputs_t remapped = {.btn = {.b = true, .z = true, .d_up = true}};
    apply_buttons(remapped, remapped.btn);
    assert(requested_noclip && !requested_give_all);
    apply_buttons(remapped, (joypad_buttons_t){0});
    assert(!requested_noclip && !requested_give_all);

    settings.cheats = false;
    settings_default_binds();
}

static void test_selection(void)
{
    for (int i = 1; i < NUM_W; i++) {
        if (W[i].id) player.weapons |= 1u << W[i].id;
    }
    cur = WEAPON_PYTHON;
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == WEAPON_MP5);
    select_weapon(WEAPON_SELECT_NEXT_POS);
    assert(pending == WEAPON_SHOTGUN);
    select_weapon(WEAPON_SELECT_NEXT_POS);
    assert(pending == WEAPON_CROSSBOW);
    select_weapon(WEAPON_SELECT_NEXT_POS);
    assert(pending == WEAPON_MP5);
    select_weapon(WEAPON_SELECT_PREV_POS);
    assert(pending == WEAPON_CROSSBOW);
    select_weapon(WEAPON_SELECT_PREV_SLOT);
    assert(pending == WEAPON_GLOCK);

    cur = WEAPON_CROWBAR;
    pending = 0;
    select_weapon(WEAPON_SELECT_PREV_SLOT);
    assert(pending == WEAPON_HANDGRENADE);
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == WEAPON_CROWBAR);

    player.weapons = (1u << WEAPON_CROWBAR) | (1u << WEAPON_CROSSBOW);
    pending = 0;
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == WEAPON_CROSSBOW); // Skip empty slot and unowned pos 0/1.
    select_weapon(WEAPON_SELECT_NEXT_POS);
    assert(pending == WEAPON_CROSSBOW); // Never leave the selected category.
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == WEAPON_CROWBAR);

    player.weapons = (1u << WEAPON_GLOCK) | (1u << WEAPON_PYTHON);
    cur = WEAPON_PYTHON;
    pending = 0;
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == WEAPON_PYTHON); // No category change means no position reset.
    select_weapon(WEAPON_SELECT_NEXT_POS);
    assert(pending == WEAPON_GLOCK);

    player.weapons = (1u << WEAPON_CROWBAR) | (1u << WEAPON_CROSSBOW);
    cur = pending = 0;
    select_weapon(WEAPON_SELECT_PREV_POS);
    assert(pending == WEAPON_CROWBAR);
    player.weapons = 0;
    cur = pending = 0;
    select_weapon(WEAPON_SELECT_NEXT_SLOT);
    assert(pending == 0);
}

static void test_saved_settings(void)
{
    settings_default_binds();
    settings.look_sens = 7;
    settings.volume = 3;
    settings.invert_y = true;
    settings.control_scheme = 4;
    settings_save();
    settings_blob_t legacy;
    memcpy(&legacy, saved_settings, sizeof(legacy));
    legacy.magic = SETTINGS_MAGIC_V1;
    memset(legacy.s.bind, 0xff, sizeof(legacy.s.bind));
    memcpy(saved_settings, &legacy, sizeof(legacy));

    settings_load();
    assert(settings.control_scheme == 1);
    assert(settings.bind[BIND_MODE_SHIFT] == PAD_A);
    assert(settings.bind[BIND_JUMP] == PAD_R);
    assert(settings.look_sens == 7 && settings.volume == 3 && settings.invert_y);
    memcpy(&legacy, saved_settings, sizeof(legacy));
    assert(legacy.magic == SETTINGS_MAGIC);

    settings_remap_bind(BIND_MODE_SHIFT, PAD_B);
    settings.control_scheme = 3;
    settings_validate_binds();
    settings_save();
    settings_t expected = settings;
    settings_default_binds();
    settings_load();
    assert(memcmp(settings.bind, expected.bind, NUM_BINDS) == 0);
    assert(settings.control_scheme == 3);

    // The previous mode-shift format included four weapon bindings between
    // normal and shifted actions. Preserve the actions while discarding those.
    memcpy(&legacy, saved_settings, sizeof(legacy));
    legacy.magic = SETTINGS_MAGIC_V2;
    const uint8_t old_bindings[12] = {
        PAD_Z, PAD_R, PAD_L, PAD_A, PAD_B,
        PAD_X, PAD_Y, PAD_DL, PAD_DR, PAD_Z, PAD_A, PAD_R
    };
    memcpy((uint8_t *)&legacy.s + offsetof(settings_t, bind), old_bindings, sizeof(old_bindings));
    memcpy(saved_settings, &legacy, sizeof(legacy));
    settings_load();
    assert(settings.bind[BIND_MODE_SHIFT] == PAD_B);
    assert(settings.bind[BIND_RELOAD] == PAD_A);
    assert(settings.bind[BIND_ALTFIRE] == PAD_Z);
    assert(settings.bind[BIND_FLASHLIGHT] == PAD_R);
    assert(settings.control_scheme == 3 && settings_weapon_pad() == PAD_CU);
    assert_valid_bindings();

    int previous_writes = writes;
    memset(saved_settings, 0, sizeof(saved_settings));
    settings_load();
    assert(writes == previous_writes); // Unrecognized saves are not overwritten.
}

int main(void)
{
    test_bindings();
    test_input();
    test_cheat_shortcuts();
    test_selection();
    test_saved_settings();
    puts("Control regressions passed: bindings, mode shift, weapon grid, save migration.");
    return 0;
}
"""

    selection_functions = "\n".join(
        function(weapons, name)
        for name in ("owned_weapon_at", "select_weapon")
    )
    source = "\n".join(
        (stand_ins, bindings, definitions, selection_functions, input_function, checks)
    )

    with tempfile.TemporaryDirectory(prefix="hl64-controls-") as temporary:
        directory = Path(temporary)
        (directory / "libdragon.h").write_text(hardware_header)
        harness = directory / "controls.c"
        harness.write_text(source)
        executable = directory / "controls.exe"
        subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", "-Wno-unused-function",
             "-Wno-unused-const-variable", "-I", str(directory), "-I", str(ROOT / "src"),
             str(harness), "-o", str(executable)],
            check=True,
        )
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
