import os
import sys
import unittest


def gui_arguments(parser, *, allow_offscreen=False):
    parser.allow_abbrev = False
    parser.add_argument("--allow-foreground", action="store_true",
                        help="Permit native windows and automated interaction after coordinating foreground access")
    args = parser.parse_args()
    offscreen = allow_offscreen and os.environ.get("QT_QPA_PLATFORM") == "offscreen"
    if not args.allow_foreground and not offscreen:
        parser.error("Native GUI checks can steal keyboard focus. Coordinate a foreground run, then pass --allow-foreground. "
                     "No profile, server, or window was opened.")
    if offscreen:
        print("Running an explicitly offscreen Qt probe, not a native-client run.", file=sys.stderr)
    return args


def offscreen_test_application(arguments):
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        raise unittest.SkipTest("Qt widget checks require explicit QT_QPA_PLATFORM=offscreen; native-client checks use --allow-foreground.")
    from aqt.qt import QApplication
    app = QApplication.instance()
    if app is not None and app.platformName() != "offscreen":
        raise unittest.SkipTest("The existing QApplication is not offscreen; refusing foreground widget checks.")
    return app or QApplication(arguments)
