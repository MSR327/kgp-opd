import sys

if len(sys.argv) == 1:  # no command: open the app window
    from kgp_opd.gui import main

    main()
else:
    from kgp_opd.cli import main

    sys.exit(main())
