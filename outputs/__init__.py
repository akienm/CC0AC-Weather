"""
Outputs: where readings go besides our own records and pages.

Each .py file in this folder is one output, such as an upload to Weather
Underground. Every reading recorded is handed to each output that is switched
on, in order, in that output's own thread, so a slow or failing output never
holds up recording or the other outputs.

An output module provides:

    NAME = "short-name"
    def enabled(cfg) -> bool          # from config.ini; is this output switched on?
    def send(cfg, reading) -> None    # called once per reading, one at a time
"""
