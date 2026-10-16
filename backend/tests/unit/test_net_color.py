import colorsys

from copperchat.views.colors import net_color


def test_stable_and_theme_dependent() -> None:
    assert net_color("GND", "agent") == net_color("GND", "agent")
    assert net_color("GND", "agent") != net_color("VCC", "agent")
    assert net_color("GND", "light") != net_color("GND", "dark")


def test_lightness_suits_background() -> None:
    for name in ("GND", "/OUT", "Net-(R1-Pad2)", "+5V"):
        r, g, b = net_color(name, "dark")
        assert colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[1] > 0.5
        r, g, b = net_color(name, "light")
        assert colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[1] < 0.5


def test_known_value_regression() -> None:
    # sha1-based; pins the algorithm so colours never drift between releases.
    assert net_color("GND", "agent") == net_color("GND", "dark")
    assert net_color("GND", "agent") == KNOWN_GND_AGENT
    assert net_color("unknown-theme-falls-back", "nope") == net_color(
        "unknown-theme-falls-back", "agent"
    )


KNOWN_GND_AGENT = (85, 175, 231)
