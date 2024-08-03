{
    'name': 'Backup pCloud',
    'version': '1.0',
    'category': 'Tools',
    'summary': 'Backup Database to pCloud',
    'description': """
        Este módulo permite realizar copias de seguridad de la base de datos en pCloud.
    """,
    'author': 'Isidro',
    'depends': ['base','web','portal','website'],
    'data': [
        'security/ir.model.access.csv',
        'data/pcloud_data.xml',
        'views/pcloud_config_views.xml',
        'views/templates.xml',
    ],
    'installable': True,
    'application': True,
}
