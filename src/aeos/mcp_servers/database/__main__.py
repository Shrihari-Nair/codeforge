from aeos.mcp_servers.common import serve
from aeos.mcp_servers.database.server import mcp

serve(mcp, default_port=8802)
