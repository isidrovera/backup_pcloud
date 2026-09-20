# -*- coding: utf-8 -*-

import os
import json
import shutil
import tempfile
import subprocess
import logging

from email.utils import parsedate_to_datetime

import requests
import odoo

from odoo import models, fields
from odoo.exceptions import ValidationError, UserError


_logger = logging.getLogger(__name__)


# ============================================================
# UTILIDADES POSTGRESQL
# ============================================================

def find_pg_tool(tool):
    """Busca una herramienta PostgreSQL instalada dentro del contenedor."""
    return shutil.which(tool)


def exec_pg_environ():
    """
    Construye las variables de entorno necesarias para que pg_dump
    pueda conectarse al PostgreSQL configurado en Odoo.
    """
    env = os.environ.copy()

    env["PGHOST"] = odoo.tools.config["db_host"] or "localhost"
    env["PGPORT"] = str(odoo.tools.config["db_port"] or 5432)
    env["PGUSER"] = odoo.tools.config["db_user"] or ""
    env["PGPASSWORD"] = odoo.tools.config["db_password"] or ""

    return env


# ============================================================
# CONFIGURACIÓN PCLOUD
# ============================================================

class PCloudConfig(models.Model):
    _name = "pcloud.configuracion"
    _description = "pCloud Configuration"

    name = fields.Char(
        string="Name",
        required=True,
    )

    client_id = fields.Char(
        string="Client ID",
        required=True,
    )

    client_secret = fields.Char(
        string="Client Secret",
        required=True,
    )

    access_token = fields.Char(
        string="Access Token",
    )

    redirect_uri = fields.Char(
        string="Redirect URI",
    )

    hostname = fields.Char(
        string="Hostname",
    )

    db_name = fields.Char(
        string="Database Name",
        required=True,
    )

    # Se conserva para no romper tu vista/configuración actual.
    # No se utiliza para pg_dump.
    master_pwd = fields.Char(
        string="Master Password",
        required=True,
    )

    backup_format = fields.Selection(
        [
            ("zip", "Zip"),
            ("dump", "Dump"),
        ],
        string="Backup Format",
        default="zip",
        required=True,
        help="Format of the backup",
    )

    notify_user = fields.Boolean(
        string="Notify User",
        help="Send an email notification when the backup succeeds or fails",
    )

    user_id = fields.Many2one(
        "res.users",
        string="User",
        help="User who receives backup notifications",
    )

    backup_filename = fields.Char(
        string="Backup Filename",
        help="Último archivo de backup generado",
        readonly=True,
    )

    generated_exception = fields.Char(
        string="Exception",
        help="Último error encontrado durante el backup",
        readonly=True,
    )

    folder_id = fields.Char(
        string="Folder ID",
        readonly=True,
    )

    main_folder_id = fields.Char(
        string="Main Folder ID",
        help=(
            "ID de la carpeta principal de pCloud "
            "para almacenar fotos de reparaciones"
        ),
    )

    # Cantidad de backups que se conservarán en pCloud.
    retention_count = fields.Integer(
        string="Backups a conservar",
        default=7,
        help=(
            "Cantidad máxima de copias de seguridad que se conservarán "
            "en la carpeta de pCloud. El backup recién generado nunca "
            "será eliminado durante su propia ejecución."
        ),
    )

    # ========================================================
    # HELPERS
    # ========================================================

    def _get_pcloud_base_url(self):
        """Devuelve el hostname de pCloud normalizado."""
        self.ensure_one()

        hostname = (self.hostname or "").strip()

        if not hostname:
            hostname = "https://api.pcloud.com"

        if not hostname.startswith(("http://", "https://")):
            hostname = "https://" + hostname

        return hostname.rstrip("/")

    def _validate_single_configuration(self):
        """
        Este módulo está diseñado para UNA configuración,
        UNA instancia Odoo y UNA base.

        - Desde botón: utiliza el registro actual.
        - Desde cron/modelo: busca la única configuración.
        - Nunca recorre configuraciones.
        """
        if self:
            if len(self) != 1:
                raise UserError(
                    "El backup debe ejecutarse sobre una sola configuración."
                )

            return self

        configs = self.search([], limit=2)

        if not configs:
            raise UserError(
                "No existe una configuración de pCloud."
            )

        if len(configs) > 1:
            raise UserError(
                "Se encontraron varias configuraciones de pCloud.\n\n"
                "Este módulo está diseñado para trabajar con una sola "
                "instancia y una sola configuración."
            )

        return configs[0]

    def _validate_configuration(self):
        """Valida los datos indispensables antes de comenzar."""
        self.ensure_one()

        if not self.db_name:
            raise ValidationError(
                "No se ha configurado el nombre de la base de datos."
            )

        if not self.access_token:
            raise ValidationError(
                "pCloud no está conectado. "
                "Utilice primero el botón 'Conectar a pCloud'."
            )

        if not self.hostname:
            raise ValidationError(
                "No existe un hostname de pCloud configurado."
            )

        pg_dump = find_pg_tool("pg_dump")

        if not pg_dump:
            raise ValidationError(
                "No se encontró el comando 'pg_dump' dentro del "
                "contenedor de Odoo."
            )

        if self.retention_count < 1:
            raise ValidationError(
                "La cantidad de backups a conservar debe ser "
                "igual o mayor a 1."
            )

    def _get_database_size(self, db_name):
        """Obtiene el tamaño aproximado de PostgreSQL."""
        try:
            db = odoo.sql_db.db_connect(db_name)

            with db.cursor() as cr:
                cr.execute(
                    "SELECT pg_database_size(%s)",
                    (db_name,),
                )

                result = cr.fetchone()

                return int(result[0] or 0)

        except Exception as exc:
            _logger.warning(
                "[PCLOUD BACKUP] No se pudo determinar tamaño DB %s: %s",
                db_name,
                exc,
            )

            return 0

    def _get_directory_size(self, path):
        """Calcula tamaño aproximado de un directorio."""
        if not path or not os.path.exists(path):
            return 0

        total = 0

        try:
            for root, dirs, files in os.walk(path):
                for filename in files:
                    file_path = os.path.join(root, filename)

                    try:
                        total += os.path.getsize(file_path)
                    except (OSError, FileNotFoundError):
                        continue

        except Exception as exc:
            _logger.warning(
                "[PCLOUD BACKUP] No se pudo calcular tamaño de %s: %s",
                path,
                exc,
            )

        return total

    @staticmethod
    def _human_size(size):
        """Convierte bytes a formato legible."""
        try:
            size = float(size or 0)
        except Exception:
            return "0 B"

        units = ["B", "KB", "MB", "GB", "TB"]

        for unit in units:
            if size < 1024:
                return "%.2f %s" % (size, unit)

            size /= 1024

        return "%.2f PB" % size

    # ========================================================
    # BLOQUEO DE EJECUCIÓN
    # ========================================================

    def _acquire_backup_lock(self):
        """
        Evita que se ejecute un segundo backup mientras el primero
        todavía está trabajando.
        """
        self.ensure_one()

        lock_name = "backup_pcloud_%s" % self.db_name

        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (lock_name,),
        )

        acquired = bool(self.env.cr.fetchone()[0])

        if not acquired:
            raise UserError(
                "Ya existe una copia de seguridad ejecutándose para "
                "la base de datos '%s'." % self.db_name
            )

        _logger.info(
            "[PCLOUD BACKUP] Lock adquirido para %s",
            self.db_name,
        )

        return lock_name

    def _release_backup_lock(self, lock_name):
        """Libera el bloqueo del backup."""
        if not lock_name:
            return

        try:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                (lock_name,),
            )

            _logger.info(
                "[PCLOUD BACKUP] Lock liberado: %s",
                lock_name,
            )

        except Exception as exc:
            _logger.warning(
                "[PCLOUD BACKUP] No se pudo liberar lock %s: %s",
                lock_name,
                exc,
            )

    # ========================================================
    # OAUTH PCLOUD
    # ========================================================

    def action_connect_to_pcloud(self):
        self.ensure_one()

        authorization_url = self.get_authorization_url()

        return {
            "type": "ir.actions.act_url",
            "url": authorization_url,
            "target": "new",
        }

    def action_disconnect_from_pcloud(self):
        self.ensure_one()

        self.write({
            "access_token": False,
            "hostname": False,
        })

        return True

    def get_authorization_url(self):
        self.ensure_one()

        url = "https://my.pcloud.com/oauth2/authorize"

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,

            # Se mantiene el comportamiento actual para no romper
            # el controlador existente.
            "state": "random_state",
        }

        return requests.Request(
            "GET",
            url,
            params=params,
        ).prepare().url

    def get_access_token(self, code):
        self.ensure_one()

        if not code:
            raise ValidationError(
                "pCloud no devolvió un código de autorización."
            )

        url = "https://api.pcloud.com/oauth2_token"

        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }

        try:
            response = requests.get(
                url,
                params=params,
                timeout=60,
            )

            data = response.json()

        except requests.RequestException as exc:
            raise ValidationError(
                "Error de conexión con pCloud: %s" % exc
            )

        except ValueError:
            raise ValidationError(
                "pCloud devolvió una respuesta inválida."
            )

        if response.status_code != 200:
            raise ValidationError(
                "Error HTTP %s al solicitar token de pCloud."
                % response.status_code
            )

        if data.get("result", 0) != 0:
            raise ValidationError(
                "pCloud rechazó la autorización: %s"
                % data
            )

        access_token = data.get("access_token")

        if not access_token:
            raise ValidationError(
                "pCloud no devolvió access_token."
            )

        hostname = data.get(
            "hostname",
            "https://api.pcloud.com",
        )

        if not hostname.startswith(("http://", "https://")):
            hostname = "https://" + hostname

        self.write({
            "access_token": access_token,
            "hostname": hostname.rstrip("/"),
        })

        _logger.info(
            "[PCLOUD BACKUP] Conexión con pCloud establecida correctamente."
        )

        return True

    # ========================================================
    # CARPETA PCLOUD
    # ========================================================

    def create_pcloud_folder(self):
        self.ensure_one()

        if not self.access_token:
            raise ValidationError(
                "No existe un token de acceso de pCloud."
            )

        folder_name = "backup_%s" % self.db_name

        url = "%s/createfolder" % self._get_pcloud_base_url()

        params = {
            "access_token": self.access_token,
            "name": folder_name,
            "folderid": 0,
        }

        _logger.info(
            "[PCLOUD BACKUP] Buscando/creando carpeta pCloud: %s",
            folder_name,
        )

        try:
            response = requests.get(
                url,
                params=params,
                timeout=60,
            )

            result = response.json()

        except requests.RequestException as exc:
            raise ValidationError(
                "No se pudo conectar con pCloud para crear "
                "la carpeta: %s" % exc
            )

        except ValueError:
            raise ValidationError(
                "Respuesta inválida de pCloud al crear carpeta."
            )

        _logger.info(
            "[PCLOUD BACKUP] Response createfolder: %s",
            result,
        )

        if (
            response.status_code == 200
            and result.get("result") == 0
            and result.get("metadata")
        ):
            folder_id = result["metadata"]["folderid"]

            self.folder_id = str(folder_id)

            return folder_id

        # pCloud: carpeta ya existente
        if result.get("result") == 2004:
            folder_id = self.get_pcloud_folder_id(
                folder_name,
            )

            self.folder_id = str(folder_id)

            return folder_id

        raise ValidationError(
            "No se pudo crear la carpeta de backup en pCloud.\n\n%s"
            % result
        )

    def get_pcloud_folder_id(self, folder_name):
        self.ensure_one()

        url = "%s/listfolder" % self._get_pcloud_base_url()

        params = {
            "access_token": self.access_token,
            "folderid": 0,
        }

        try:
            response = requests.get(
                url,
                params=params,
                timeout=60,
            )

            result = response.json()

        except requests.RequestException as exc:
            raise ValidationError(
                "Error consultando las carpetas de pCloud: %s"
                % exc
            )

        except ValueError:
            raise ValidationError(
                "Respuesta inválida de pCloud al consultar carpetas."
            )

        _logger.info(
            "[PCLOUD BACKUP] Response listfolder root: %s",
            result,
        )

        if response.status_code != 200:
            raise ValidationError(
                "pCloud respondió HTTP %s."
                % response.status_code
            )

        if result.get("result") != 0:
            raise ValidationError(
                "pCloud devolvió un error al listar carpetas: %s"
                % result
            )

        metadata = result.get("metadata") or {}

        for item in metadata.get("contents", []):
            if (
                item.get("isfolder") == 1
                and item.get("name") == folder_name
            ):
                return item.get("folderid")

        raise ValidationError(
            "Carpeta no encontrada en pCloud: %s"
            % folder_name
        )

    # ========================================================
    # SUBIDA PCLOUD
    # ========================================================

    def upload_file_to_pcloud(self, file_path, folder_id):
        self.ensure_one()

        if not self.access_token:
            raise ValidationError(
                "No existe un access token de pCloud."
            )

        if not os.path.isfile(file_path):
            raise ValidationError(
                "El archivo de backup no existe:\n%s"
                % file_path
            )

        file_size = os.path.getsize(file_path)

        if file_size <= 0:
            raise ValidationError(
                "El archivo de backup tiene tamaño 0."
            )

        _logger.info(
            "[PCLOUD BACKUP] Iniciando upload: %s | tamaño=%s",
            os.path.basename(file_path),
            self._human_size(file_size),
        )

        url = "%s/uploadfile" % self._get_pcloud_base_url()

        params = {
            "access_token": self.access_token,
            "folderid": folder_id,
        }

        try:
            with open(file_path, "rb") as file_obj:
                response = requests.post(
                    url,
                    params=params,
                    files={"file": file_obj},

                    # 60 segundos para conexión.
                    # 2 horas para transferencia de archivo grande.
                    timeout=(60, 7200),
                )

            result = response.json()

        except requests.Timeout:
            raise ValidationError(
                "La subida a pCloud excedió el tiempo máximo."
            )

        except requests.RequestException as exc:
            raise ValidationError(
                "Error durante la subida a pCloud: %s"
                % exc
            )

        except ValueError:
            raise ValidationError(
                "pCloud devolvió una respuesta inválida "
                "después de subir el backup."
            )

        _logger.info(
            "[PCLOUD BACKUP] Response uploadfile: %s",
            result,
        )

        if response.status_code != 200:
            raise ValidationError(
                "La subida a pCloud respondió HTTP %s."
                % response.status_code
            )

        if result.get("result") != 0:
            raise ValidationError(
                "pCloud rechazó el archivo de backup:\n%s"
                % result
            )

        metadata = result.get("metadata") or []

        if not metadata:
            raise ValidationError(
                "pCloud confirmó la petición pero no devolvió "
                "metadata del archivo."
            )

        uploaded = metadata[0]

        file_id = uploaded.get("fileid")
        remote_size = int(uploaded.get("size") or 0)

        if not file_id:
            raise ValidationError(
                "pCloud no devolvió fileid para el backup."
            )

        if remote_size and remote_size != file_size:
            raise ValidationError(
                "El tamaño del archivo subido a pCloud no coincide.\n\n"
                "Local: %s\n"
                "pCloud: %s"
                % (
                    self._human_size(file_size),
                    self._human_size(remote_size),
                )
            )

        _logger.info(
            "[PCLOUD BACKUP] Upload completado correctamente. "
            "file_id=%s tamaño=%s",
            file_id,
            self._human_size(remote_size or file_size),
        )

        return {
            "file_id": file_id,
            "size": remote_size or file_size,
            "name": uploaded.get("name"),
            "metadata": uploaded,
        }

    # ========================================================
    # RETENCIÓN / ELIMINACIÓN DE BACKUPS
    # ========================================================

    @staticmethod
    def _parse_pcloud_date(value):
        """
        Convierte:
            Sun, 20 Sep 2026 12:58:04 +0000

        en un datetime real.

        Esto corrige el bug anterior donde las fechas se ordenaban
        alfabéticamente como strings.
        """
        if not value:
            return None

        try:
            return parsedate_to_datetime(value)
        except Exception:
            return None

    def _delete_pcloud_file(self, file_id, filename=None):
        self.ensure_one()

        url = "%s/deletefile" % self._get_pcloud_base_url()

        params = {
            "access_token": self.access_token,
            "fileid": file_id,
        }

        _logger.info(
            "[PCLOUD BACKUP] Eliminando backup antiguo: %s | file_id=%s",
            filename or "sin nombre",
            file_id,
        )

        try:
            response = requests.get(
                url,
                params=params,
                timeout=60,
            )

            result = response.json()

        except requests.RequestException as exc:
            raise ValidationError(
                "No se pudo eliminar un backup antiguo "
                "de pCloud: %s" % exc
            )

        except ValueError:
            raise ValidationError(
                "pCloud devolvió una respuesta inválida "
                "al eliminar el backup."
            )

        _logger.info(
            "[PCLOUD BACKUP] Response deletefile: %s",
            result,
        )

        if response.status_code != 200:
            raise ValidationError(
                "Error HTTP %s eliminando backup antiguo."
                % response.status_code
            )

        if result.get("result") != 0:
            raise ValidationError(
                "pCloud no pudo eliminar el backup antiguo:\n%s"
                % result
            )

        return True

    def cleanup_old_backups(
        self,
        folder_id,
        protected_file_id=None,
    ):
        """
        Mantiene solamente retention_count backups.

        IMPORTANTE:
        - Ordena usando datetime real.
        - Nunca elimina el backup que acaba de generarse.
        - Elimina tantos antiguos como sean necesarios.
        """
        self.ensure_one()

        retention = max(
            int(self.retention_count or 7),
            1,
        )

        url = "%s/listfolder" % self._get_pcloud_base_url()

        params = {
            "access_token": self.access_token,
            "folderid": folder_id,
        }

        try:
            response = requests.get(
                url,
                params=params,
                timeout=60,
            )

            result = response.json()

        except requests.RequestException as exc:
            raise ValidationError(
                "No se pudo consultar la carpeta de backups: %s"
                % exc
            )

        except ValueError:
            raise ValidationError(
                "pCloud devolvió una respuesta inválida "
                "al consultar backups."
            )

        _logger.info(
            "[PCLOUD BACKUP] Response listfolder cleanup: %s",
            result,
        )

        if response.status_code != 200:
            raise ValidationError(
                "HTTP %s consultando backups de pCloud."
                % response.status_code
            )

        if result.get("result") != 0:
            raise ValidationError(
                "pCloud devolvió error al consultar backups:\n%s"
                % result
            )

        metadata = result.get("metadata") or {}
        contents = metadata.get("contents") or []

        backups = []

        for item in contents:
            if item.get("isfolder"):
                continue

            filename = item.get("name") or ""

            # Solo archivos correspondientes a esta base.
            # Evita eliminar accidentalmente otros archivos.
            expected_prefix = "%s_" % self.db_name

            if not filename.startswith(expected_prefix):
                _logger.warning(
                    "[PCLOUD BACKUP] Archivo ignorado durante retención "
                    "porque no pertenece a esta base: %s",
                    filename,
                )
                continue

            file_id = item.get("fileid")

            created = self._parse_pcloud_date(
                item.get("created")
            )

            if not created:
                _logger.warning(
                    "[PCLOUD BACKUP] No se pudo interpretar fecha "
                    "del archivo %s. No será eliminado automáticamente.",
                    filename,
                )
                continue

            backups.append({
                "fileid": file_id,
                "name": filename,
                "created": created,
                "size": int(item.get("size") or 0),
            })

        # Más antiguo → más nuevo.
        backups.sort(
            key=lambda item: item["created"]
        )

        _logger.info(
            "[PCLOUD BACKUP] Backups válidos encontrados=%s | "
            "retención=%s | protegido=%s",
            len(backups),
            retention,
            protected_file_id,
        )

        if len(backups) <= retention:
            _logger.info(
                "[PCLOUD BACKUP] No es necesario eliminar backups."
            )

            return True

        number_to_delete = len(backups) - retention

        candidates = [
            item
            for item in backups
            if str(item.get("fileid"))
            != str(protected_file_id)
        ]

        deleted = 0

        for item in candidates:
            if deleted >= number_to_delete:
                break

            self._delete_pcloud_file(
                item["fileid"],
                filename=item["name"],
            )

            deleted += 1

        if deleted < number_to_delete:
            _logger.warning(
                "[PCLOUD BACKUP] Se necesitaban eliminar %s backups, "
                "pero solo fue seguro eliminar %s.",
                number_to_delete,
                deleted,
            )

        _logger.info(
            "[PCLOUD BACKUP] Limpieza terminada. "
            "Backups eliminados=%s",
            deleted,
        )

        return True

    # Compatibilidad con cualquier llamada antigua.
    def delete_oldest_backup(
        self,
        folder_id,
        protected_file_id=None,
    ):
        self.ensure_one()

        return self.cleanup_old_backups(
            folder_id,
            protected_file_id=protected_file_id,
        )

    # ========================================================
    # GENERACIÓN DEL BACKUP
    # ========================================================

    def dump_data(
        self,
        db_name,
        stream,
        backup_format,
    ):
        self.ensure_one()

        pg_dump = find_pg_tool("pg_dump")

        if not pg_dump:
            raise ValidationError(
                "No se encontró pg_dump."
            )

        env = exec_pg_environ()

        _logger.info(
            "[PCLOUD BACKUP] DUMP DB: %s | format=%s",
            db_name,
            backup_format,
        )

        # ====================================================
        # BACKUP ZIP COMPATIBLE CON ODOO
        # ====================================================

        if backup_format == "zip":

            with tempfile.TemporaryDirectory(
                prefix="odoo_backup_dump_"
            ) as dump_dir:

                dump_sql = os.path.join(
                    dump_dir,
                    "dump.sql",
                )

                filestore_source = (
                    odoo.tools.config.filestore(db_name)
                )

                cmd = [
                    pg_dump,
                    "--no-owner",
                    "--file=%s" % dump_sql,
                    db_name,
                ]

                _logger.info(
                    "[PCLOUD BACKUP] Ejecutando pg_dump para %s",
                    db_name,
                )

                process = subprocess.run(
                    cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                if process.returncode != 0:
                    stderr = (
                        process.stderr.decode(
                            "utf-8",
                            errors="replace",
                        )
                        if process.stderr
                        else "Sin información adicional"
                    )

                    raise ValidationError(
                        "pg_dump falló para la base '%s'.\n\n%s"
                        % (
                            db_name,
                            stderr[-4000:],
                        )
                    )

                if not os.path.isfile(dump_sql):
                    raise ValidationError(
                        "pg_dump terminó pero no creó dump.sql."
                    )

                dump_size = os.path.getsize(dump_sql)

                if dump_size <= 0:
                    raise ValidationError(
                        "El archivo dump.sql generado está vacío."
                    )

                _logger.info(
                    "[PCLOUD BACKUP] PostgreSQL generado correctamente. "
                    "dump.sql=%s",
                    self._human_size(dump_size),
                )

                # --------------------------------------------
                # FILESTORE
                # --------------------------------------------

                if os.path.exists(filestore_source):

                    filestore_destination = os.path.join(
                        dump_dir,
                        "filestore",
                    )

                    filestore_size = (
                        self._get_directory_size(
                            filestore_source
                        )
                    )

                    _logger.info(
                        "[PCLOUD BACKUP] Copiando filestore. "
                        "origen=%s | tamaño aproximado=%s",
                        filestore_source,
                        self._human_size(filestore_size),
                    )

                    shutil.copytree(
                        filestore_source,
                        filestore_destination,
                    )

                    _logger.info(
                        "[PCLOUD BACKUP] Filestore copiado correctamente."
                    )

                else:
                    _logger.warning(
                        "[PCLOUD BACKUP] No existe filestore para %s "
                        "en %s",
                        db_name,
                        filestore_source,
                    )

                # --------------------------------------------
                # MANIFEST ODOO
                # --------------------------------------------

                manifest_file = os.path.join(
                    dump_dir,
                    "manifest.json",
                )

                db = odoo.sql_db.db_connect(db_name)

                with db.cursor() as cr:
                    manifest = self._dump_db_manifest(cr)

                with open(
                    manifest_file,
                    "w",
                    encoding="utf-8",
                ) as fh:
                    json.dump(
                        manifest,
                        fh,
                        indent=4,
                        ensure_ascii=False,
                    )

                # --------------------------------------------
                # ZIP FINAL
                # --------------------------------------------

                _logger.info(
                    "[PCLOUD BACKUP] Comprimiendo backup ZIP..."
                )

                if stream:

                    odoo.tools.osutil.zip_dir(
                        dump_dir,
                        stream,
                        include_dir=False,
                        fnct_sort=lambda filename: (
                            filename != "dump.sql"
                        ),
                    )

                    return True

                temporary_file = tempfile.TemporaryFile()

                odoo.tools.osutil.zip_dir(
                    dump_dir,
                    temporary_file,
                    include_dir=False,
                    fnct_sort=lambda filename: (
                        filename != "dump.sql"
                    ),
                )

                temporary_file.seek(0)

                return temporary_file

        # ====================================================
        # BACKUP DUMP POSTGRESQL
        # ====================================================

        cmd = [
            pg_dump,
            "--no-owner",
            "--format=c",
            db_name,
        ]

        process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = process.communicate()

        if process.returncode != 0:
            error_text = (
                stderr.decode(
                    "utf-8",
                    errors="replace",
                )
                if stderr
                else "Sin información adicional"
            )

            raise ValidationError(
                "pg_dump falló para la base '%s'.\n\n%s"
                % (
                    db_name,
                    error_text[-4000:],
                )
            )

        if not stdout:
            raise ValidationError(
                "pg_dump generó un archivo vacío."
            )

        if stream:
            stream.write(stdout)

            return True

        return stdout

    # ========================================================
    # MANIFEST ODOO
    # ========================================================

    def _dump_db_manifest(self, cr):
        pg_version = "%d.%d" % divmod(
            int(
                cr._obj.connection.server_version
            ) // 100,
            100,
        )

        cr.execute(
            """
            SELECT name, latest_version
            FROM ir_module_module
            WHERE state = 'installed'
            """
        )

        modules = dict(cr.fetchall())

        return {
            "odoo_dump": "1",
            "db_name": cr.dbname,
            "version": odoo.release.version,
            "version_info": odoo.release.version_info,
            "major_version": odoo.release.major_version,
            "pg_version": pg_version,
            "modules": modules,
        }

    # ========================================================
    # NOTIFICACIONES
    # ========================================================

    def _send_backup_notification(self, success=True):
        self.ensure_one()

        if not self.notify_user:
            return

        try:
            if success:
                template = self.env.ref(
                    "backup_pcloud.mail_template_data_db_backup_success",
                    raise_if_not_found=False,
                )
            else:
                template = self.env.ref(
                    "backup_pcloud.mail_template_data_db_backup_failed",
                    raise_if_not_found=False,
                )

            if not template:
                _logger.warning(
                    "[PCLOUD BACKUP] No existe plantilla de correo "
                    "para success=%s",
                    success,
                )

                return

            template.send_mail(
                self.id,
                force_send=True,
            )

        except Exception as exc:
            # Una falla de correo NO debe convertir un backup válido
            # en backup fallido.
            _logger.exception(
                "[PCLOUD BACKUP] Error enviando notificación: %s",
                exc,
            )

    # ========================================================
    # BACKUP PRINCIPAL
    # ========================================================

    def backup_database(self):
        """
        Ejecuta el backup de UNA sola instancia/configuración.

        Compatible con:
        - botón de Odoo;
        - acción servidor;
        - ir.cron.

        NO recorre configuraciones.
        """

        record = self._validate_single_configuration()

        record.ensure_one()
        record._validate_configuration()

        lock_name = None

        _logger.info(
            "============================================================"
        )

        _logger.info(
            "[PCLOUD BACKUP] INICIO BACKUP | configuración=%s | DB=%s",
            record.name,
            record.db_name,
        )

        try:
            lock_name = record._acquire_backup_lock()

            record.generated_exception = False

            # ------------------------------------------------
            # INFORMACIÓN PREVIA
            # ------------------------------------------------

            database_size = record._get_database_size(
                record.db_name
            )

            filestore_path = (
                odoo.tools.config.filestore(
                    record.db_name
                )
            )

            filestore_size = record._get_directory_size(
                filestore_path
            )

            temp_root = tempfile.gettempdir()
            disk_usage = shutil.disk_usage(temp_root)

            _logger.info(
                "[PCLOUD BACKUP] DB aproximada=%s | "
                "Filestore aproximado=%s | "
                "Libre en %s=%s",
                record._human_size(database_size),
                record._human_size(filestore_size),
                temp_root,
                record._human_size(disk_usage.free),
            )

            # Solo advertimos, no bloqueamos.
            estimated_source = (
                database_size + filestore_size
            )

            if (
                estimated_source
                and disk_usage.free < estimated_source
            ):
                _logger.warning(
                    "[PCLOUD BACKUP] ATENCIÓN: el espacio libre temporal "
                    "(%s) es inferior al tamaño aproximado DB+filestore "
                    "(%s). El backup podría fallar por falta de espacio.",
                    record._human_size(
                        disk_usage.free
                    ),
                    record._human_size(
                        estimated_source
                    ),
                )

            # ------------------------------------------------
            # NOMBRE DEL ARCHIVO
            # ------------------------------------------------

            backup_time = (
                fields.Datetime.now().strftime(
                    "%Y-%m-%d_%H-%M-%S"
                )
            )

            backup_filename = (
                "%s_%s.%s"
                % (
                    record.db_name,
                    backup_time,
                    record.backup_format,
                )
            )

            record.backup_filename = backup_filename

            _logger.info(
                "[PCLOUD BACKUP] Archivo=%s",
                backup_filename,
            )

            # ------------------------------------------------
            # TEMPORAL
            # ------------------------------------------------

            with tempfile.TemporaryDirectory(
                prefix="odoo_pcloud_backup_"
            ) as temp_dir:

                backup_file = os.path.join(
                    temp_dir,
                    backup_filename,
                )

                _logger.info(
                    "[PCLOUD BACKUP] Generando backup local: %s",
                    backup_file,
                )

                with open(
                    backup_file,
                    "wb",
                ) as file_obj:

                    record.dump_data(
                        record.db_name,
                        file_obj,
                        record.backup_format,
                    )

                # --------------------------------------------
                # VALIDACIÓN ARCHIVO
                # --------------------------------------------

                if not os.path.exists(backup_file):
                    raise ValidationError(
                        "El archivo de backup no fue generado."
                    )

                backup_size = os.path.getsize(
                    backup_file
                )

                if backup_size <= 0:
                    raise ValidationError(
                        "El archivo de backup generado está vacío."
                    )

                _logger.info(
                    "[PCLOUD BACKUP] Backup local generado. "
                    "tamaño=%s",
                    record._human_size(
                        backup_size
                    ),
                )

                # --------------------------------------------
                # CARPETA
                # --------------------------------------------

                folder_id = (
                    record.folder_id
                    or record.create_pcloud_folder()
                )

                # --------------------------------------------
                # UPLOAD
                # --------------------------------------------

                uploaded = (
                    record.upload_file_to_pcloud(
                        backup_file,
                        folder_id,
                    )
                )

                uploaded_file_id = uploaded[
                    "file_id"
                ]

                _logger.info(
                    "[PCLOUD BACKUP] Backup confirmado en pCloud. "
                    "file_id=%s",
                    uploaded_file_id,
                )

                # --------------------------------------------
                # RETENCIÓN
                # --------------------------------------------

                record.cleanup_old_backups(
                    folder_id,
                    protected_file_id=uploaded_file_id,
                )

            # ------------------------------------------------
            # ÉXITO
            # ------------------------------------------------

            record.generated_exception = False

            _logger.info(
                "[PCLOUD BACKUP] BACKUP COMPLETADO CORRECTAMENTE | "
                "DB=%s | archivo=%s",
                record.db_name,
                backup_filename,
            )

            record._send_backup_notification(
                success=True,
            )

            _logger.info(
                "============================================================"
            )

            return True

        except Exception as exc:

            error_message = str(exc)

            record.generated_exception = (
                error_message[:2000]
            )

            _logger.exception(
                "[PCLOUD BACKUP] ERROR BACKUP | DB=%s | %s",
                record.db_name,
                error_message,
            )

            record._send_backup_notification(
                success=False,
            )

            _logger.info(
                "============================================================"
            )

            # IMPORTANTE:
            # Volvemos a lanzar el error para que el botón/cron
            # marque realmente que hubo un problema.
            raise

        finally:
            if lock_name:
                record._release_backup_lock(
                    lock_name
                )