from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class PCloudController(http.Controller):

    @http.route('/auth/callback', type='http', auth='public', website=True, csrf=False)
    def pcloud_callback(self, **kwargs):
        code = kwargs.get('code')
        state = kwargs.get('state')

        pcloud_config = request.env['pcloud.configuracion'].search([], limit=1)
        if not pcloud_config:
            return "Configuración de pCloud no encontrada."

        try:
            pcloud_config.get_access_token(code)
            return request.render('backup_pcloud.pcloud_success', {})
        except Exception as e:
            return request.render('backup_pcloud.pcloud_error', {'error': str(e)})

    @http.route('/backup/execute', type='http', auth='user', website=True)
    def execute_backup(self, **kwargs):
        pcloud_config = request.env['pcloud.configuracion'].search([], limit=1)
        if not pcloud_config:
            return "Configuración de pCloud no encontrada."
        
        try:
            pcloud_config.backup_database()
            return "Copia de seguridad completada y subida a pCloud"
        except Exception as e:
            _logger.error("Error during backup: %s", str(e))
            return str(e)
