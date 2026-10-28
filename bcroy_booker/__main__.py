import sys

if len(sys.argv) == 1:  # no command: open the app window
    from bcroy_booker.gui import main

    main()
else:
    from bcroy_booker.cli import main

    sys.exit(main())
