from pathlib import Path
import csv

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent

ARCHIVO_ENTRADA = (
    BASE_DIR
    / "datos"
    / "transacciones_raw.csv"
)

ARCHIVO_LIMPIO = (
    BASE_DIR
    / "datos"
    / "transacciones_limpias.csv"
)

ARCHIVO_OBSERVADO = (
    BASE_DIR
    / "datos"
    / "transacciones_observadas.csv"
)


CAMPOS_CRITICOS = [
    "id_transaccion",
    "monto",
    "divisa",
    "fecha",
    "tipo",
]


def normalizar_texto(valor):
    if pd.isna(valor):
        return None

    texto = str(valor).strip()

    if texto == "":
        return None

    return texto


def normalizar_divisa(valor):
    valor = normalizar_texto(valor)

    if valor is None:
        return None

    return valor.upper()


def normalizar_tipo(valor):
    valor = normalizar_texto(valor)

    if valor is None:
        return None

    return valor.upper()


def normalizar_monto(valor):
    valor = normalizar_texto(valor)

    if valor is None:
        return None

    texto = valor.replace(",", ".")

    try:
        monto = float(texto)
    except ValueError:
        return None

    return round(monto, 2)


def normalizar_fecha(valor):
    valor = normalizar_texto(valor)

    if valor is None:
        return None

    fecha = pd.to_datetime(
        valor,
        errors="coerce",
        dayfirst=True,
        format="mixed",
    )

    if pd.isna(fecha):
        return None

    return fecha.strftime("%Y-%m-%d")


def detectar_delimitador(
    archivo_entrada,
):
    """
    Detecta si el archivo CSV utiliza ',' o ';'
    como delimitador.

    Si no puede detectarlo automáticamente,
    intenta inferirlo desde la primera línea.
    """

    ruta = Path(
        archivo_entrada
    )

    with ruta.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as archivo:

        muestra = archivo.read(
            4096
        )

    if not muestra.strip():
        raise ValueError(
            "El archivo CSV está vacío."
        )

    try:
        dialecto = csv.Sniffer().sniff(
            muestra,
            delimiters=",;",
        )

        return dialecto.delimiter

    except csv.Error:
        primera_linea = (
            muestra
            .splitlines()[0]
        )

        cantidad_punto_coma = (
            primera_linea.count(";")
        )

        cantidad_coma = (
            primera_linea.count(",")
        )

        if (
            cantidad_punto_coma
            > cantidad_coma
        ):
            return ";"

        if cantidad_coma > 0:
            return ","

        raise ValueError(
            "No se pudo detectar el delimitador "
            "del archivo CSV. "
            "Se admiten ',' y ';'."
        )


def cargar_csv(
    archivo_entrada,
):
    """
    Lee un CSV aceptando delimitadores ',' o ';'.
    """

    delimitador = detectar_delimitador(
        archivo_entrada
    )

    df = pd.read_csv(
        archivo_entrada,
        dtype=str,
        sep=delimitador,
        encoding="utf-8-sig",
    )

    # Limpia espacios accidentales en los nombres
    # de las columnas.
    df.columns = [
        str(columna).strip()
        for columna in df.columns
    ]

    return df


def construir_motivo_rechazo(fila):
    motivos = []

    id_transaccion = fila.get(
        "id_transaccion"
    )

    if (
        id_transaccion is None
        or pd.isna(id_transaccion)
    ):
        motivos.append(
            "id_transaccion_nulo"
        )

    monto_original = fila.get(
        "_monto_original"
    )

    monto = fila.get(
        "monto"
    )

    if (
        monto_original is None
        or pd.isna(monto_original)
    ):
        motivos.append(
            "monto_nulo"
        )

    elif (
        monto is None
        or pd.isna(monto)
    ):
        motivos.append(
            "monto_invalido"
        )

    elif monto < 0:
        motivos.append(
            "monto_negativo"
        )

    divisa = fila.get(
        "divisa"
    )

    if (
        divisa is None
        or pd.isna(divisa)
    ):
        motivos.append(
            "divisa_nulo"
        )

    fecha_original = fila.get(
        "_fecha_original"
    )

    fecha = fila.get(
        "fecha"
    )

    if (
        fecha_original is None
        or pd.isna(fecha_original)
    ):
        motivos.append(
            "fecha_nulo"
        )

    elif (
        fecha is None
        or pd.isna(fecha)
    ):
        motivos.append(
            "fecha_invalida"
        )

    tipo = fila.get(
        "tipo"
    )

    if (
        tipo is None
        or pd.isna(tipo)
    ):
        motivos.append(
            "tipo_nulo"
        )

    if fila.get(
        "_duplicado",
        False,
    ):
        motivos.append(
            "id_transaccion_duplicado"
        )

    motivos = list(
        dict.fromkeys(motivos)
    )

    if not motivos:
        return None

    return ";".join(motivos)


def transformar(
    archivo_entrada=ARCHIVO_ENTRADA,
    archivo_limpio=ARCHIVO_LIMPIO,
    archivo_observado=ARCHIVO_OBSERVADO,
):
    archivo_entrada = Path(
        archivo_entrada
    )

    if not archivo_entrada.exists():
        raise FileNotFoundError(
            f"No existe el archivo: "
            f"{archivo_entrada}"
        )

    df = cargar_csv(
        archivo_entrada
    )

    columnas_faltantes = [
        campo
        for campo in CAMPOS_CRITICOS
        if campo not in df.columns
    ]

    if columnas_faltantes:
        raise ValueError(
            "Faltan columnas obligatorias: "
            + ", ".join(
                columnas_faltantes
            )
            + ". Columnas encontradas: "
            + ", ".join(
                df.columns
            )
        )

    registros_entrada = len(
        df
    )

    # Conservamos algunos valores originales
    # para poder identificar qué tipo de error
    # ocurrió.
    df["_monto_original"] = (
        df["monto"]
        .apply(
            normalizar_texto
        )
    )

    df["_fecha_original"] = (
        df["fecha"]
        .apply(
            normalizar_texto
        )
    )

    # Limpieza general de ID.
    df["id_transaccion"] = (
        df["id_transaccion"]
        .apply(
            normalizar_texto
        )
    )

    # Normalización.
    df["monto"] = (
        df["monto"]
        .apply(
            normalizar_monto
        )
    )

    df["divisa"] = (
        df["divisa"]
        .apply(
            normalizar_divisa
        )
    )

    df["fecha"] = (
        df["fecha"]
        .apply(
            normalizar_fecha
        )
    )

    df["tipo"] = (
        df["tipo"]
        .apply(
            normalizar_tipo
        )
    )

    # Detectamos IDs duplicados.
    #
    # Se marcan todas las apariciones del ID
    # duplicado, no solamente la segunda,
    # para evitar decidir arbitrariamente
    # cuál registro es correcto.
    df["_duplicado"] = (
        df["id_transaccion"]
        .notna()
        & df["id_transaccion"]
        .duplicated(
            keep=False
        )
    )

    df["motivo_rechazo"] = (
        df.apply(
            construir_motivo_rechazo,
            axis=1,
        )
    )

    observados = df[
        df["motivo_rechazo"]
        .notna()
    ].copy()

    limpios = df[
        df["motivo_rechazo"]
        .isna()
    ].copy()

    columnas_salida = [
        "id_transaccion",
        "monto",
        "divisa",
        "fecha",
        "tipo",
    ]

    columnas_observados = (
        columnas_salida
        + [
            "motivo_rechazo"
        ]
    )

    limpios = limpios[
        columnas_salida
    ]

    observados = observados[
        columnas_observados
    ]

    Path(
        archivo_limpio
    ).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    limpios.to_csv(
        archivo_limpio,
        index=False,
    )

    observados.to_csv(
        archivo_observado,
        index=False,
    )

    print(
        "ETL completado"
    )

    print(
        f"Archivo procesado: "
        f"{archivo_entrada.name}"
    )

    print(
        f"Delimitador detectado: "
        f"'{detectar_delimitador(archivo_entrada)}'"
    )

    print(
        f"Registros recibidos: "
        f"{registros_entrada}"
    )

    print(
        f"Registros válidos: "
        f"{len(limpios)}"
    )

    print(
        f"Registros observados: "
        f"{len(observados)}"
    )

    return {
        "limpios": limpios,
        "observados": observados,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "ETL SmartBancs: "
            "python -m etl.transformar "
            "[archivo.csv]"
        )
    )

    parser.add_argument(
        "archivo",
        nargs="?",
        default=ARCHIVO_ENTRADA,
        help=(
            "CSV de entrada "
            "(por defecto "
            "etl/datos/transacciones_raw.csv)"
        ),
    )

    argumentos = (
        parser.parse_args()
    )

    transformar(
        archivo_entrada=argumentos.archivo
    )