"""TimeLens web UI entry point. Start it with `timelens ui`, which binds it to localhost only.

A plain `streamlit run` on this file listens on every network interface, and the file
pickers can read any local path, so do not start it that way.
"""

import streamlit as st

from timelens.ui import pages

st.set_page_config(page_title="TimeLens", page_icon=":material/schedule:")
st.navigation({
    "Process model": [
        st.Page(pages.check, title="Check rules", icon=":material/rule:", default=True),
        st.Page(pages.repair, title="Repair rules", icon=":material/build:"),
    ],
    "Event log": [
        st.Page(pages.explore, title="Explore a log", icon=":material/table_view:"),
        st.Page(pages.calibrate, title="Calibrate ranges", icon=":material/tune:"),
        st.Page(pages.frontier, title="Risk against cost", icon=":material/show_chart:"),
        st.Page(pages.alerts, title="Replay with alerts", icon=":material/notifications:"),
    ],
    "Performance": [
        st.Page(pages.bench, title="Benchmark", icon=":material/speed:"),
    ],
}).run()
