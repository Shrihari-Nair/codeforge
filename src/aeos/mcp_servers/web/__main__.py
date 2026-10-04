from aeos.mcp_servers.common import serve
from aeos.mcp_servers.web.server import mcp

serve(mcp, default_port=8804)
