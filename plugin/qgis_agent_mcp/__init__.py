def classFactory(iface):
    from .plugin import QgisAgentMcpPlugin

    return QgisAgentMcpPlugin(iface)

