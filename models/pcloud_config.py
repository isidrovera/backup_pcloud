import requests
import logging
import tempfile
import os
import shutil
import subprocess
import json
import odoo
from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

def find_pg_tool(tool):
    return shutil.which(tool)

def exec_pg_environ():
    env = os.environ.copy()
    env['PGHOST'] = odoo.tools.config['db_host'] or 'localhost'
    env['PGPORT'] = str(odoo.tools.config['db_port'] or 5432)
    env['PGUSER'] = odoo.tools.config['db_user'] or ''
    env['PGPASSWORD'] = odoo.tools.config['db_password'] or ''
    return env

class PCloudConfig(models.Model):
    _name = 'pcloud.configuracion'
    _description = 'pCloud Configuration'

    name = fields.Char(string='Name', required=True)
    client_id = fields.Char(string='Client ID', required=True)
    client_secret = fields.Char(string='Client Secret', required=True)
    access_token = fields.Char(string='Access Token')
    redirect_uri = fields.Char(string='Redirect URI')
    hostname = fields.Char(string='Hostname')
    db_name = fields.Char(string='Database Name', required=True)
    master_pwd = fields.Char(string='Master Password', required=True)
    backup_format = fields.Selection([
        ('zip', 'Zip'),
        ('dump', 'Dump')
    ], string='Backup Format', default='zip', required=True, help='Format of the backup')
    notify_user = fields.Boolean(string='Notify User', help='Send an email notification to user when the backup operation is successful or failed')
    user_id = fields.Many2one('res.users', string='User', help='Name of the user')
    backup_filename = fields.Char(string='Backup Filename', help='For Storing generated backup filename')
    generated_exception = fields.Char(string='Exception', help='Exception Encountered while Backup generation')
    folder_id = fields.Char(string='Folder ID', readonly=True)

    def action_connect_to_pcloud(self):
        for record in self:
            authorization_url = record.get_authorization_url()
            return {
                'type': 'ir.actions.act_url',
                'url': authorization_url,
                'target': 'new',
            }

    def action_disconnect_from_pcloud(self):
        for record in self:
            record.access_token = False
            record.hostname = False

    def get_authorization_url(self):
        for record in self:
            url = "https://my.pcloud.com/oauth2/authorize"
            params = {
                'client_id': record.client_id,
                'response_type': 'code',
                'redirect_uri': record.redirect_uri,
                'state': 'random_state'
            }
            return requests.Request('GET', url, params=params).prepare().url

    def get_access_token(self, code):
        for record in self:
            url = "https://api.pcloud.com/oauth2_token"
            params = {
                'client_id': record.client_id,
                'client_secret': record.client_secret,
                'code': code,
                'redirect_uri': record.redirect_uri
            }
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                record.access_token = data['access_token']
                record.hostname = data.get('hostname', 'https://api.pcloud.com')
            else:
                raise Exception("Failed to get access token")

    def create_pcloud_folder(self):
        for record in self:
            if not record.access_token:
                raise Exception("No access token found. Please connect to pCloud first.")
            
            folder_name = f"backup_{record.db_name}"
            url = f"{record.hostname}/createfolder"
            params = {
                'access_token': record.access_token,
                'name': folder_name,
                'folderid': 0  # 0 para crear en la raíz
            }
            response = requests.get(url, params=params)
            result = response.json()
            _logger.info("Response from createfolder: %s", result)
            if response.status_code == 200 and 'metadata' in result:
                folder_id = result['metadata']['folderid']
                record.folder_id = folder_id
                return folder_id
            elif result.get('result') == 2004:  # Folder already exists
                folder_id = self.get_pcloud_folder_id(folder_name, record)
                record.folder_id = folder_id
                return folder_id
            else:
                raise Exception(f"Failed to create folder: {result}")

    def get_pcloud_folder_id(self, folder_name, record):
        try:
            url = f"{record.hostname}/listfolder"
            params = {
                'access_token': record.access_token,
                'folderid': 0  # Root directory
            }
            response = requests.get(url, params=params)
            result = response.json()
            _logger.info("Response from listfolder: %s", result)
            if result['result'] == 0:
                for item in result['metadata']['contents']:
                    if item['isfolder'] == 1 and item['name'] == folder_name:
                        return item['folderid']
            raise ValidationError("Carpeta no encontrada en pCloud: %s" % folder_name)
        except Exception as e:
            raise ValidationError("Error al obtener el ID de la carpeta en pCloud: %s" % str(e))

    def upload_file_to_pcloud(self, file_path, folder_id):
        for record in self:
            if not record.access_token:
                raise Exception("No access token found. Please connect to pCloud first.")
            
            url = f"{record.hostname}/uploadfile"
            params = {
                'access_token': record.access_token,
                'folderid': folder_id,
            }
            with open(file_path, 'rb') as file:
                files = {'file': file}
                response = requests.post(url, params=params, files=files)
                result = response.json()
                _logger.info("Response from uploadfile: %s", result)
                if response.status_code == 200:
                    return result['metadata'][0]['fileid']
                else:
                    raise Exception("Failed to upload file")

    def delete_oldest_backup(self, folder_id):
        for record in self:
            if not record.access_token:
                raise Exception("No access token found. Please connect to pCloud first.")
            
            url = f"{record.hostname}/listfolder"
            params = {
                'access_token': record.access_token,
                'folderid': folder_id
            }
            response = requests.get(url, params=params)
            result = response.json()
            _logger.info("Response from listfolder: %s", result)
            if response.status_code == 200:
                contents = result['metadata']['contents']
                backups = [item for item in contents if not item['isfolder']]
                backups.sort(key=lambda x: x['created'])
                if len(backups) > 3:
                    file_id = backups[0]['fileid']
                    delete_url = f"{record.hostname}/deletefile"
                    delete_params = {
                        'access_token': record.access_token,
                        'fileid': file_id
                    }
                    delete_response = requests.get(delete_url, params=delete_params)
                    delete_result = delete_response.json()
                    _logger.info("Response from deletefile: %s", delete_result)
                    if delete_response.status_code != 200:
                        raise Exception("Failed to delete oldest backup")
            else:
                raise Exception("Failed to list folder contents")

    def dump_data(self, db_name, stream, backup_format):
        _logger.info('DUMP DB: %s format %s', db_name, backup_format)
        cmd = [find_pg_tool('pg_dump'), '--no-owner', db_name]
        env = exec_pg_environ()
        if backup_format == 'zip':
            with tempfile.TemporaryDirectory() as dump_dir:
                filestore = odoo.tools.config.filestore(db_name)
                cmd.append('--file=' + os.path.join(dump_dir, 'dump.sql'))
                subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True)
                if os.path.exists(filestore):
                    shutil.copytree(filestore, os.path.join(dump_dir, 'filestore'))
                with open(os.path.join(dump_dir, 'manifest.json'), 'w') as fh:
                    db = odoo.sql_db.db_connect(db_name)
                    with db.cursor() as cr:
                        json.dump(self._dump_db_manifest(cr), fh, indent=4)
                if stream:
                    odoo.tools.osutil.zip_dir(dump_dir, stream, include_dir=False, fnct_sort=lambda file_name: file_name != 'dump.sql')
                else:
                    t = tempfile.TemporaryFile()
                    odoo.tools.osutil.zip_dir(dump_dir, t, include_dir=False, fnct_sort=lambda file_name: file_name != 'dump.sql')
                    t.seek(0)
                    return t
        else:
            cmd.append('--format=c')
            process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE)
            stdout, _ = process.communicate()
            if stream:
                stream.write(stdout)
            else:
                return stdout

    def _dump_db_manifest(self, cr):
        pg_version = "%d.%d" % divmod(cr._obj.connection.server_version / 100, 100)
        cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
        modules = dict(cr.fetchall())
        manifest = {
            'odoo_dump': '1',
            'db_name': cr.dbname,
            'version': odoo.release.version,
            'version_info': odoo.release.version_info,
            'major_version': odoo.release.major_version,
            'pg_version': pg_version,
            'modules': modules,
        }
        return manifest

    def backup_database(self):
        _logger.info("Cron job started: Backup Database")
        for record in self.search([]):
            try:
                _logger.info("Processing backup for record: %s", record.name)
                backup_time = fields.Datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                backup_filename = "%s_%s.%s" % (record.db_name, backup_time, record.backup_format)
                record.backup_filename = backup_filename

                with tempfile.TemporaryDirectory() as temp_dir:
                    backup_file = os.path.join(temp_dir, backup_filename)
                    _logger.info("Dumping database to %s", backup_file)
                    with open(backup_file, "wb") as f:
                        self.dump_data(record.db_name, f, record.backup_format)

                    folder_id = record.folder_id or record.create_pcloud_folder()
                    record.upload_file_to_pcloud(backup_file, folder_id)
                    record.delete_oldest_backup(folder_id)

                    _logger.info("Backup process completed for %s", record.db_name)
            except Exception as e:
                _logger.error('Exception during backup: %s', e)
                record.generated_exception = str(e)
                if record.notify_user:
                    mail_template = self.env.ref('backup_pcloud.mail_template_data_db_backup_failed')
                    mail_template.send_mail(record.id, force_send=True)
