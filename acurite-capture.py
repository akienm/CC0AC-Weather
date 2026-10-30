#!/usr/bin/env python3
"""acurite-capture.py — starts the station. The program is the cc0ac package (cc0ac/capture.py);
this file stays so the service file, the boot loader and every `python3 acurite-capture.py` keep working."""
from cc0ac.capture import main

if __name__ == "__main__":
    main()
