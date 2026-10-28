"""Entry point for the double-click app: no arguments opens the window; with arguments it
behaves like the `bcroy-book` command (scheduled runs use this)."""
import sys

sys.argv = [a for a in sys.argv if not a.startswith("-psn_")]  # old macOS adds a process id
if len(sys.argv) == 1:
    from bcroy_booker.gui import main

    main()
else:
    from bcroy_booker.cli import main

    sys.exit(main())
