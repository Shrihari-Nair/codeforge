from aeos.mcp_servers.common import serve
from aeos.mcp_servers.observability.server import mcp

serve(mcp, default_port=8801)
