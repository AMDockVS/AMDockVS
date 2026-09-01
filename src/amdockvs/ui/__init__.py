__all__ = ["AMDockVSMainWindow"]


def __getattr__(name: str):
    if name == "AMDockVSMainWindow":
        from amdockvs.ui.main_window import AMDockVSMainWindow

        return AMDockVSMainWindow
    raise AttributeError(name)
