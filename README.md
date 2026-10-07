vbi_update_item.py

Script de cambios masivos sobre items de FOLIO (inventario), con modo
PREVIEW (no modifica nada) y modo EJECUCIÓN (--execute), generando reportes
detallados en ambos casos.

Reglas de negocio aplicadas a cada item del CSV de entrada
------------------------------------------------------------
1) itemLevelCallNumberTypeId se fuerza siempre a
   "95467209-6d7b-468b-94df-0f5d7ad2747d" (Library of Congress classification),
   sin importar el tipo de clasificación actual del item.

2) Se mueve itemLevelCallNumberPrefix (Prefixo de classificação) hacia
   itemLevelCallNumber (Número de chamada), haciendo trim previo de ambos
   valores para decidir el caso:
     - Número vacío y prefijo con contenido -> número = prefijo
       (caso "copiar_prefijo").
     - Ambos con contenido -> número = "prefijo número"
       (caso "concatenar").
     - Prefijo vacío y número con contenido -> sin cambios
       (caso "sin_cambio").
     - Ambos vacíos -> número = "P" (caso "ambos_vacios_P").
   En todos los casos, el campo itemLevelCallNumberPrefix se elimina del
   item (ver constante CLEAR_PREFIX).

Modos de ejecución
-------------------
- Por defecto el script corre en modo PREVIEW: lee, valida y simula los
  cambios, pero NO hace ningún PUT contra FOLIO.
- Con --execute se aplican los cambios reales. Antes de modificar nada se
  pide confirmación escribiendo "SI" (se puede omitir con --yes), y se
  guarda un backup JSON de los items originales que se van a modificar.
- --limit N permite procesar solamente los primeros N UUID válidos del CSV
  (útil para pruebas).

Entrada (CSV)
-------------
Por defecto se busca "items_to_update.csv" junto al script, luego en
"settings/" y luego en el directorio actual (configurable con
--items-csv). Se tolera BOM utf-8, delimitador "," ";" o tabulador, y
líneas vacías. Se reconoce la columna de UUID por encabezado (alias:
"Item UUID", item_uuid, item_id, itemId, uuid, id); si no hay encabezado
reconocible se usa la primera columna. Los valores con formato de UUID
inválido quedan registrados en el reporte como UUID_INVALIDO, y los
duplicados se eliminan conservando la primera aparición.

Autenticación
--------------
Se reutiliza folio_login_module.py (cookies de sesión + reintento
automático en 401). Las credenciales se leen de
settings/okapi_customers.json (carpeta "settings", junto al script), con
la estructura:
    {"okapi": [{"libraryName": ..., "x_okapi_url": ..., "x_okapi_tenant": ...,
                "userName": ..., "password": ...}]}
El libraryName se recibe como argumento posicional; si se omite, se
solicita por consola.

Reportes (carpeta reports/, creada si no existe)
--------------------------------------------------
- rvbi_<preview|execute>_<libraryName>_<timestamp>.csv (utf-8-sig), con
  columnas: item_id, hrid, barcode, status, material_type,
  old_call_number_type, new_call_number_type, old_prefix, new_prefix,
  old_call_number, new_call_number, type_changed, rule2_case, result,
  detail.
- rvbi_<modo>_<libraryName>_<timestamp>_summary.txt con el resumen de la
  corrida (conteos de UUID válidos/inválidos, no encontrados, errores de
  lectura, total de filas, items con cambios, conteo de la regla 1 y de
  cada caso de la regla 2, y -en ejecución- actualizados y errores).

Resultado posible por fila: PENDIENTE, SIN_CAMBIOS, ACTUALIZADO, ERROR,
UUID_INVALIDO, NO_ENCONTRADO, ERROR_LECTURA.
"""
