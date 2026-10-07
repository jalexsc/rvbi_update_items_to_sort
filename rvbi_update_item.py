#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rvbi_update_item.py

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

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from folio_login_module import folio_login_module

# -----------------------------
# Configuración / constantes
# -----------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_CUSTOMERS_FILE = "okapi_customers.json"
DEFAULT_ITEMS_CSV = "items_to_update.csv"
DEFAULT_RPS = 5.0
RETRY_STATUSES = {429, 502, 503, 504, 505}
GET_BATCH_SIZE = 50  # tamaño de lote para GET /inventory/items

# Regla 1: tipo de clasificación destino (Library of Congress classification)
NEW_CALL_NUMBER_TYPE_ID = "95467209-6d7b-468b-94df-0f5d7ad2747d"
# Regla 2: si True, el campo itemLevelCallNumberPrefix se elimina del item
CLEAR_PREFIX = True

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
UUID_HEADER_ALIASES = {"itemuuid", "item_uuid", "item_id", "itemid", "uuid", "id"}

REPORT_COLUMNS = [
    "item_id", "hrid", "barcode", "status", "material_type",
    "old_call_number_type", "new_call_number_type",
    "old_prefix", "new_prefix",
    "old_call_number", "new_call_number",
    "type_changed", "rule2_case", "result", "detail",
]

RULE2_CASES = ("copiar_prefijo", "concatenar", "sin_cambio", "ambos_vacios_P")


# -----------------------------
# Throttler (RPS)
# -----------------------------
class Throttler:
    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self.last_call = 0.0

    def wait(self) -> None:
        if not self.min_interval:
            return
        elapsed = time.time() - self.last_call
        sleep_time = self.min_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
        self.last_call = time.time()


# -----------------------------
# Request con retry/backoff (GET/PUT)
# -----------------------------
def request_with_retry(
    client: folio_login_module,
    throttler: Throttler,
    method: str,
    path: str,
    max_retries: int = 5,
    base_delay: float = 1.0,
    **kwargs,
):
    """
    Envía una request vía folio_login_module con reintentos.

    - Reintenta (con backoff exponencial) ante excepciones de red y ante
      códigos en RETRY_STATUSES.
    - Para cualquier otro status code (incluyendo 4xx/5xx no reintentables)
      devuelve la respuesta tal cual, para que el llamador decida.
    """
    method = method.upper()
    attempt = 0
    delay = base_delay

    while True:
        throttler.wait()
        try:
            if method == "GET":
                resp = client.get(path, **kwargs)
            elif method == "PUT":
                resp = client.put(path, **kwargs)
            else:
                raise ValueError(f"Método no soportado: {method}")
        except Exception as exc:
            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(f"{method} {path} falló tras {max_retries} intentos: {exc}")
            logging.warning(
                f"Excepción en {method} {path} (intento {attempt}/{max_retries}): {exc}. "
                f"Reintentando en {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        if resp.status_code in RETRY_STATUSES and attempt < max_retries:
            attempt += 1
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            logging.warning(
                f"{resp.status_code} en {method} {path} (intento {attempt}/{max_retries}) "
                f"-> espera {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        return resp


def detect_list_payload(js: Any) -> Tuple[Optional[str], Optional[List[Any]]]:
    """Encuentra el primer campo tipo lista dentro de una respuesta FOLIO."""
    if not isinstance(js, dict):
        return None, None
    for k, v in js.items():
        if isinstance(v, list):
            return k, v
    return None, None


def chunked(seq: List[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# -----------------------------
# Resolución de archivos (script_dir, settings/, cwd)
# -----------------------------
def resolve_file(explicit: str, default_name: str, extra_dirs: List[Path]) -> Path:
    """
    Busca un archivo probando, en orden: la ruta explícita tal cual, y luego
    esa misma ruta (o el nombre por defecto) combinada con cada directorio
    en extra_dirs.
    """
    name = explicit or default_name
    candidates = [Path(name)]
    for d in extra_dirs:
        candidates.append(d / name)

    for c in candidates:
        if c.exists():
            return c

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"No se encontró '{name}'. Rutas probadas: {tried}")


# -----------------------------
# Lectura y validación del CSV de entrada
# -----------------------------
def detect_delimiter(line: str) -> str:
    counts = {d: line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def normalize_header_cell(cell: str) -> str:
    return re.sub(r"[\s_]+", "", cell.strip().lower())


def load_items_csv(path: Path) -> List[Tuple[str, Optional[str]]]:
    """
    Lee el CSV de UUID a procesar.

    Returns:
        Lista de tuplas (valor_original, uuid_normalizado_o_None). Si el
        valor no tiene formato de UUID válido, el segundo elemento es None.
    """
    raw_text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in raw_text.splitlines() if ln.strip() != ""]
    if not lines:
        return []

    delimiter = detect_delimiter(lines[0])
    first_cells = [c.strip().strip('"') for c in lines[0].split(delimiter)]

    header_idx = None
    for i, cell in enumerate(first_cells):
        if normalize_header_cell(cell) in UUID_HEADER_ALIASES:
            header_idx = i
            break

    data_lines = lines[1:] if header_idx is not None else lines
    col_idx = header_idx if header_idx is not None else 0

    entries: List[Tuple[str, Optional[str]]] = []
    for ln in data_lines:
        cells = [c.strip().strip('"') for c in ln.split(delimiter)]
        value = cells[col_idx] if col_idx < len(cells) else (cells[0] if cells else "")
        value = value.strip()
        if not value:
            continue
        normalized = value.lower() if UUID_RE.match(value) else None
        entries.append((value, normalized))

    return entries


# -----------------------------
# Mapas de referencia: call-number-types / material-types
# -----------------------------
def fetch_id_name_map(client: folio_login_module, throttler: Throttler, path: str) -> Dict[str, str]:
    try:
        resp = request_with_retry(client, throttler, "GET", path, params={"limit": 1000})
    except RuntimeError as exc:
        logging.warning(f"No se pudo consultar {path}: {exc}")
        return {}

    if resp.status_code >= 400:
        logging.warning(f"No se pudo consultar {path}: HTTP {resp.status_code} - {resp.text[:200]}")
        return {}

    try:
        js = resp.json()
    except ValueError:
        logging.warning(f"Respuesta no JSON al consultar {path}")
        return {}

    _, rows = detect_list_payload(js)
    if not isinstance(rows, list):
        return {}

    return {r["id"]: r.get("name", "") for r in rows if isinstance(r, dict) and r.get("id")}


# -----------------------------
# Reglas de negocio
# -----------------------------
def apply_rules(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aplica, sobre `item` (que debe ser una copia independiente del original),
    la regla 1 (tipo de clasificación) y la regla 2 (mover prefijo a número
    de llamada). Modifica `item` in-place y devuelve el detalle de cambios.
    """
    old_type_id = item.get("itemLevelCallNumberTypeId") or ""
    raw_number = item.get("itemLevelCallNumber") or ""
    raw_prefix = item.get("itemLevelCallNumberPrefix") or ""
    old_number = raw_number.strip()
    old_prefix = raw_prefix.strip()

    # --- Regla 1: forzar tipo de clasificación, sin condición de origen ---
    item["itemLevelCallNumberTypeId"] = NEW_CALL_NUMBER_TYPE_ID
    type_changed = old_type_id != NEW_CALL_NUMBER_TYPE_ID

    # --- Regla 2: mover prefijo -> número de llamada ---
    if not old_number and old_prefix:
        new_number = old_prefix
        rule2_case = "copiar_prefijo"
    elif old_number and old_prefix:
        new_number = f"{old_prefix} {old_number}"
        rule2_case = "concatenar"
    elif old_number and not old_prefix:
        # Sin cambios: se conserva el valor original tal cual (sin trim forzado)
        new_number = raw_number
        rule2_case = "sin_cambio"
    else:
        new_number = "P"
        rule2_case = "ambos_vacios_P"

    if rule2_case != "sin_cambio":
        item["itemLevelCallNumber"] = new_number

    if CLEAR_PREFIX:
        item.pop("itemLevelCallNumberPrefix", None)
    new_prefix_display = "" if CLEAR_PREFIX else raw_prefix

    rule2_changed = rule2_case != "sin_cambio"
    changed = type_changed or rule2_changed

    return {
        "old_type_id": old_type_id,
        "new_type_id": NEW_CALL_NUMBER_TYPE_ID,
        "type_changed": type_changed,
        "old_prefix": raw_prefix,
        "new_prefix": new_prefix_display,
        "old_number": raw_number,
        "new_number": new_number,
        "rule2_case": rule2_case,
        "changed": changed,
    }


# -----------------------------
# Construcción de filas de reporte
# -----------------------------
def build_blank_row(item_id: str, result: str, detail: str = "") -> Dict[str, str]:
    row = {col: "" for col in REPORT_COLUMNS}
    row["item_id"] = item_id
    row["result"] = result
    row["detail"] = detail
    return row


def build_item_row(
    uuid: str,
    item: Dict[str, Any],
    rule_result: Dict[str, Any],
    cn_type_names: Dict[str, str],
    material_type_names: Dict[str, str],
    result: str,
    detail: str = "",
) -> Dict[str, str]:
    material_id = item.get("materialTypeId") or ""
    material_name = material_type_names.get(material_id, material_id)
    old_type_name = cn_type_names.get(rule_result["old_type_id"], rule_result["old_type_id"])
    new_type_name = cn_type_names.get(rule_result["new_type_id"], rule_result["new_type_id"])

    return {
        "item_id": uuid,
        "hrid": item.get("hrid", ""),
        "barcode": item.get("barcode", ""),
        "status": (item.get("status") or {}).get("name", ""),
        "material_type": material_name,
        "old_call_number_type": old_type_name,
        "new_call_number_type": new_type_name,
        "old_prefix": rule_result["old_prefix"],
        "new_prefix": rule_result["new_prefix"],
        "old_call_number": rule_result["old_number"],
        "new_call_number": rule_result["new_number"],
        "type_changed": "SI" if rule_result["type_changed"] else "NO",
        "rule2_case": rule_result["rule2_case"],
        "result": result,
        "detail": detail,
    }


# -----------------------------
# Resumen (.txt)
# -----------------------------
def build_summary_lines(
    mode_word: str,
    library: str,
    items_csv_path: Path,
    counts: Dict[str, Any],
    is_execute: bool,
) -> List[str]:
    lines = [
        f"Modo: {mode_word.upper()}",
        f"Biblioteca: {library}",
        f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Archivo CSV de entrada: {items_csv_path}",
        f"UUID válidos (con duplicados): {counts['valid']}",
        f"UUID inválidos: {counts['invalid']}",
        f"Duplicados eliminados: {counts['duplicates_removed']}",
    ]
    if counts.get("skipped_by_limit"):
        lines.append(f"Omitidos por --limit: {counts['skipped_by_limit']}")
    lines.extend(
        [
            f"Ítems no encontrados en FOLIO: {counts['not_found']}",
            f"Errores de lectura (GET): {counts['read_errors']}",
            f"Total de filas en el CSV: {counts['total_rows']}",
            f"Ítems con cambios: {counts['items_with_changes']}",
            f"Regla 1 (cambio de tipo de clasificación): {counts['rule1_count']}",
            "Regla 2 (mover prefijo a número de llamada):",
            f"  - copiar_prefijo: {counts['rule2']['copiar_prefijo']}",
            f"  - concatenar: {counts['rule2']['concatenar']}",
            f"  - sin_cambio: {counts['rule2']['sin_cambio']}",
            f"  - ambos_vacios_P: {counts['rule2']['ambos_vacios_P']}",
        ]
    )
    if is_execute:
        lines.append(f"Ítems actualizados: {counts['updated']}")
        lines.append(f"Errores al actualizar: {counts['errors']}")
    return lines


# -----------------------------
# MAIN
# -----------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Actualiza masivamente itemLevelCallNumberTypeId y mueve "
            "itemLevelCallNumberPrefix -> itemLevelCallNumber en items de FOLIO."
        )
    )
    parser.add_argument(
        "libraryName",
        nargs="?",
        default=None,
        help="Bloque libraryName en okapi_customers.json (si se omite, se solicita por consola)",
    )
    parser.add_argument(
        "--customers",
        default=DEFAULT_CUSTOMERS_FILE,
        help="Nombre/ruta del archivo de credenciales (default: settings/okapi_customers.json junto al script)",
    )
    parser.add_argument(
        "--items-csv",
        default=DEFAULT_ITEMS_CSV,
        help="Nombre/ruta del CSV con los Item UUID a procesar (default: items_to_update.csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Procesar solo los primeros N UUID válidos del CSV (pruebas)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Aplica los cambios en FOLIO (por defecto: preview, no modifica nada)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Omite la confirmación interactiva en modo --execute",
    )
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS, help="Requests per second (0 = sin límite)")
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.loglevel).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    # --- libraryName: argumento o prompt interactivo ---
    library = (args.libraryName or "").strip()
    while not library:
        library = input("Ingrese el libraryName (bloque en okapi_customers.json): ").strip()

    # --- Resolución de archivos de configuración ---
    try:
        customers_path = resolve_file(
            args.customers, DEFAULT_CUSTOMERS_FILE, [SCRIPT_DIR / "settings", SCRIPT_DIR, Path.cwd()]
        )
        items_csv_path = resolve_file(
            args.items_csv, DEFAULT_ITEMS_CSV, [SCRIPT_DIR, SCRIPT_DIR / "settings", Path.cwd()]
        )
    except FileNotFoundError as exc:
        logging.error(str(exc))
        return 1

    logging.info(f"Credenciales: {customers_path}")
    logging.info(f"CSV de items: {items_csv_path}")

    # --- Carga y validación de UUID del CSV ---
    entries = load_items_csv(items_csv_path)
    total_rows = len(entries)

    invalid_entries = [(raw, norm) for raw, norm in entries if norm is None]
    valid_entries = [(raw, norm) for raw, norm in entries if norm is not None]

    seen = set()
    unique_valid: List[str] = []
    for _raw, norm in valid_entries:
        if norm not in seen:
            seen.add(norm)
            unique_valid.append(norm)
    duplicates_removed = len(valid_entries) - len(unique_valid)

    if args.limit is not None and args.limit >= 0:
        processed_uuids = unique_valid[: args.limit]
    else:
        processed_uuids = unique_valid
    skipped_by_limit = len(unique_valid) - len(processed_uuids)

    logging.info(
        f"CSV: {total_rows} filas | válidos={len(valid_entries)} inválidos={len(invalid_entries)} "
        f"duplicados_eliminados={duplicates_removed} a_procesar={len(processed_uuids)} "
        f"omitidos_por_limit={skipped_by_limit}"
    )

    report_rows: List[Dict[str, str]] = []
    for raw, _norm in invalid_entries:
        report_rows.append(build_blank_row(raw, "UUID_INVALIDO", "Formato UUID inválido"))

    # --- Conexión/autenticación centralizada vía folio_login_module ---
    client = folio_login_module(library, customers_json_path=str(customers_path))
    throttler = Throttler(args.rps)

    # --- Mapas de referencia (nombres de tipo de clasificación y material) ---
    cn_type_names = fetch_id_name_map(client, throttler, "/call-number-types")
    material_type_names = fetch_id_name_map(client, throttler, "/material-types")

    if cn_type_names and NEW_CALL_NUMBER_TYPE_ID not in cn_type_names:
        logging.warning(
            f"ADVERTENCIA: el UUID {NEW_CALL_NUMBER_TYPE_ID} (Library of Congress classification) "
            f"no existe en /call-number-types del tenant '{library}'."
        )

    # --- Lectura por lotes y aplicación de reglas ---
    not_found_count = 0
    read_error_count = 0
    rule1_count = 0
    items_with_changes = 0
    rule2_counts = {case: 0 for case in RULE2_CASES}
    items_to_update: List[Dict[str, Any]] = []

    for batch in chunked(processed_uuids, GET_BATCH_SIZE):
        cql = "id==(" + " or ".join(f'"{u}"' for u in batch) + ")"

        try:
            resp = request_with_retry(
                client, throttler, "GET", "/inventory/items", params={"query": cql, "limit": len(batch)}
            )
        except RuntimeError as exc:
            for u in batch:
                report_rows.append(build_blank_row(u, "ERROR_LECTURA", str(exc)))
            read_error_count += len(batch)
            continue

        if resp.status_code >= 400:
            detail = f"HTTP {resp.status_code}: {resp.text[:300]}"
            for u in batch:
                report_rows.append(build_blank_row(u, "ERROR_LECTURA", detail))
            read_error_count += len(batch)
            continue

        try:
            js = resp.json()
        except ValueError:
            for u in batch:
                report_rows.append(build_blank_row(u, "ERROR_LECTURA", "Respuesta no es JSON válido"))
            read_error_count += len(batch)
            continue

        _, rows = detect_list_payload(js)
        items_by_id: Dict[str, Dict[str, Any]] = {}
        if isinstance(rows, list):
            for it in rows:
                if isinstance(it, dict) and it.get("id"):
                    items_by_id[str(it["id"]).lower()] = it

        for u in batch:
            item = items_by_id.get(u)
            if item is None:
                report_rows.append(
                    build_blank_row(u, "NO_ENCONTRADO", "El ítem no fue devuelto por /inventory/items")
                )
                not_found_count += 1
                continue

            modified_item = copy.deepcopy(item)
            rule_result = apply_rules(modified_item)

            if rule_result["type_changed"]:
                rule1_count += 1
            rule2_counts[rule_result["rule2_case"]] += 1

            if rule_result["changed"]:
                items_with_changes += 1
                result_status = "PENDIENTE"
            else:
                result_status = "SIN_CAMBIOS"

            row = build_item_row(u, item, rule_result, cn_type_names, material_type_names, result_status)
            report_rows.append(row)

            if rule_result["changed"]:
                items_to_update.append(
                    {"uuid": u, "original_item": item, "modified_item": modified_item, "row": row}
                )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_word = "execute" if args.execute else "preview"
    updated_count = 0
    error_count = 0

    # --- Modo ejecución: confirmación, backup y PUT ---
    if args.execute:
        print(
            f"\nSe aplicarán cambios a {len(items_to_update)} ítem(s) de {len(processed_uuids)} "
            f"procesados en la biblioteca '{library}'."
        )
        if not args.yes:
            answer = input('Escriba "SI" para confirmar la ejecución: ').strip().upper()
            if answer != "SI":
                logging.info("Operación cancelada por el usuario. No se realizó ningún cambio.")
                return 1

        if items_to_update:
            backups_dir = Path("backups")
            backups_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backups_dir / f"rvbi_backup_{library}_{timestamp}.json"
            backup_data = [entry["original_item"] for entry in items_to_update]
            backup_path.write_text(json.dumps(backup_data, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info(f"Backup guardado: {backup_path} ({len(backup_data)} ítems)")

        for entry in items_to_update:
            uuid = entry["uuid"]
            row = entry["row"]
            try:
                resp = request_with_retry(
                    client, throttler, "PUT", f"/inventory/items/{uuid}", json=entry["modified_item"]
                )
            except RuntimeError as exc:
                row["result"] = "ERROR"
                row["detail"] = str(exc)
                error_count += 1
                continue

            if resp.status_code in (200, 204):
                row["result"] = "ACTUALIZADO"
                row["detail"] = ""
                updated_count += 1
            else:
                row["result"] = "ERROR"
                row["detail"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
                error_count += 1

    # --- Reportes ---
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    csv_path = reports_dir / f"rvbi_{mode_word}_{library}_{timestamp}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(report_rows)

    counts = {
        "valid": len(valid_entries),
        "invalid": len(invalid_entries),
        "duplicates_removed": duplicates_removed,
        "skipped_by_limit": skipped_by_limit,
        "not_found": not_found_count,
        "read_errors": read_error_count,
        "total_rows": total_rows,
        "items_with_changes": items_with_changes,
        "rule1_count": rule1_count,
        "rule2": rule2_counts,
        "updated": updated_count,
        "errors": error_count,
    }
    summary_lines = build_summary_lines(mode_word, library, items_csv_path, counts, args.execute)

    summary_path = reports_dir / f"rvbi_{mode_word}_{library}_{timestamp}_summary.txt"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"Resumen ({mode_word.upper()}) - {library}")
    print("=" * 70)
    for line in summary_lines:
        print(line)
    print("=" * 70)
    print(f"Reporte CSV: {csv_path}")
    print(f"Reporte resumen: {summary_path}")
    if not args.execute:
        print("MODO PREVIEW: no se modificó ningún ítem en FOLIO.")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
