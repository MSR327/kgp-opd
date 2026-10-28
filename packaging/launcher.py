"""Entry point for the double-click app: no arguments opens the window; with arguments it
behaves like the `kgp-opd` command (scheduled runs use this)."""
import sys

sys.argv = [a for a in sys.argv if not a.startswith("-psn_")]  # old macOS adds a process id
if len(sys.argv) == 1:
    from kgp_opd.gui import main

    main()
else:
    from kgp_opd.cli import main

    sys.exit(main())
