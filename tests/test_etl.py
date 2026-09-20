import pandas as pd

from etl.transformar import (
    normalizar_divisa,
    normalizar_fecha,
    normalizar_monto,
    normalizar_texto,
    normalizar_tipo,
    transformar,
)


def test_normalizar_texto():
    assert normalizar_texto("  hola  ") == "hola"
    assert normalizar_texto("") is None
    assert normalizar_texto("   ") is None
    assert normalizar_texto(None) is None


def test_normalizar_divisa():
    assert normalizar_divisa("usd") == "USD"
    assert normalizar_divisa("Usd") == "USD"
    assert normalizar_divisa(" USD ") == "USD"
    assert normalizar_divisa(None) is None


def test_normalizar_tipo():
    assert normalizar_tipo("transferencia") == "TRANSFERENCIA"
    assert normalizar_tipo(" Pago ") == "PAGO"
    assert normalizar_tipo(None) is None


def test_normalizar_monto():
    assert normalizar_monto("100.50") == 100.50
    assert normalizar_monto("50,25") == 50.25
    assert normalizar_monto(" 30.10 ") == 30.10

    assert normalizar_monto("abc") is None
    assert normalizar_monto(None) is None


def test_normalizar_fecha():
    assert normalizar_fecha("2026-09-19") == "2026-09-19"
    assert normalizar_fecha("19-09-2026") == "2026-09-19"
    assert normalizar_fecha("2026/09/19") == "2026-09-19"

    assert normalizar_fecha("fecha-invalida") is None
    assert normalizar_fecha(None) is None


def test_transformar_separa_limpios_y_observados(tmp_path):
    entrada = tmp_path / "transacciones_raw.csv"
    salida_limpia = tmp_path / "transacciones_limpias.csv"
    salida_observada = tmp_path / "transacciones_observadas.csv"

    datos = pd.DataFrame(
        [
            # válido
            {
                "id_transaccion": "1",
                "monto": "100.50",
                "divisa": "usd",
                "fecha": "2026/09/19",
                "tipo": "transferencia",
            },

            # monto nulo
            {
                "id_transaccion": "2",
                "monto": None,
                "divisa": "USD",
                "fecha": "19-09-2026",
                "tipo": "TRANSFERENCIA",
            },

            # válido con coma decimal
            {
                "id_transaccion": "3",
                "monto": "50,25",
                "divisa": "Usd",
                "fecha": "2026-09-19",
                "tipo": "pago",
            },

            # monto negativo
            {
                "id_transaccion": "4",
                "monto": "-20",
                "divisa": "USD",
                "fecha": "2026-09-19",
                "tipo": "transferencia",
            },

            # divisa nula
            {
                "id_transaccion": "5",
                "monto": "75.00",
                "divisa": None,
                "fecha": "2026-09-19",
                "tipo": "deposito",
            },

            # monto inválido
            {
                "id_transaccion": "6",
                "monto": "abc",
                "divisa": "USD",
                "fecha": "2026-09-19",
                "tipo": "pago",
            },

            # fecha inválida
            {
                "id_transaccion": "7",
                "monto": "25.00",
                "divisa": "USD",
                "fecha": "fecha-mala",
                "tipo": "transferencia",
            },

            # duplicado 1
            {
                "id_transaccion": "8",
                "monto": "90.00",
                "divisa": "USD",
                "fecha": "2026-09-19",
                "tipo": "deposito",
            },

            # duplicado 2
            {
                "id_transaccion": "8",
                "monto": "95.00",
                "divisa": "USD",
                "fecha": "2026-09-19",
                "tipo": "deposito",
            },

            # válido con espacios
            {
                "id_transaccion": "9",
                "monto": "120.00",
                "divisa": " usd ",
                "fecha": "2026/09/20",
                "tipo": " transferencia ",
            },
        ]
    )

    datos.to_csv(
        entrada,
        index=False,
    )

    resultado = transformar(
        archivo_entrada=entrada,
        archivo_limpio=salida_limpia,
        archivo_observado=salida_observada,
    )

    assert salida_limpia.exists()
    assert salida_observada.exists()

    limpios = pd.read_csv(
        salida_limpia,
        dtype={"id_transaccion": str},
    )

    observados = pd.read_csv(
        salida_observada,
        dtype={"id_transaccion": str},
    )

    # -----------------------------
    # VALIDAR REGISTROS LIMPIOS
    # -----------------------------

    ids_limpios = set(
        limpios["id_transaccion"].tolist()
    )

    assert ids_limpios == {
        "1",
        "3",
        "9",
    }

    # Divisas normalizadas
    assert set(
        limpios["divisa"]
    ) == {"USD"}

    # Tipos normalizados
    assert set(
        limpios["tipo"]
    ) == {
        "TRANSFERENCIA",
        "PAGO",
    }

    # Montos correctos
    monto_1 = limpios.loc[
        limpios["id_transaccion"] == "1",
        "monto",
    ].iloc[0]

    assert monto_1 == 100.50

    monto_3 = limpios.loc[
        limpios["id_transaccion"] == "3",
        "monto",
    ].iloc[0]

    assert monto_3 == 50.25

    # Fechas ISO
    assert all(
        len(fecha) == 10
        and fecha[4] == "-"
        and fecha[7] == "-"
        for fecha in limpios["fecha"]
    )

    # -----------------------------
    # VALIDAR OBSERVADOS
    # -----------------------------

    ids_observados = set(
        observados["id_transaccion"].tolist()
    )

    assert ids_observados == {
        "2",
        "4",
        "5",
        "6",
        "7",
        "8",
    }

    # Monto nulo
    motivo_2 = observados.loc[
        observados["id_transaccion"] == "2",
        "motivo_rechazo",
    ].iloc[0]

    assert "monto_nulo" in motivo_2

    # Monto negativo
    motivo_4 = observados.loc[
        observados["id_transaccion"] == "4",
        "motivo_rechazo",
    ].iloc[0]

    assert "monto_negativo" in motivo_4

    # Divisa nula
    motivo_5 = observados.loc[
        observados["id_transaccion"] == "5",
        "motivo_rechazo",
    ].iloc[0]

    assert "divisa_nulo" in motivo_5

    # Monto inválido
    motivo_6 = observados.loc[
        observados["id_transaccion"] == "6",
        "motivo_rechazo",
    ].iloc[0]

    assert "monto_invalido" in motivo_6

    # Fecha inválida
    motivo_7 = observados.loc[
        observados["id_transaccion"] == "7",
        "motivo_rechazo",
    ].iloc[0]

    assert "fecha_invalida" in motivo_7

    # Ambos duplicados deben observarse
    duplicados = observados[
        observados["id_transaccion"] == "8"
    ]

    assert len(duplicados) == 2

    assert all(
        "id_transaccion_duplicado"
        in motivo
        for motivo in duplicados["motivo_rechazo"]
    )

    # -----------------------------
    # VALIDAR RETORNO
    # -----------------------------

    assert len(
        resultado["limpios"]
    ) == 3

    assert len(
        resultado["observados"]
    ) == 7


def test_transformar_rechaza_columnas_faltantes(tmp_path):
    entrada = tmp_path / "incompleto.csv"
    limpio = tmp_path / "limpio.csv"
    observado = tmp_path / "observado.csv"

    datos = pd.DataFrame(
        [
            {
                "id_transaccion": "1",
                "monto": "10.00",
            }
        ]
    )

    datos.to_csv(
        entrada,
        index=False,
    )

    try:
        transformar(
            archivo_entrada=entrada,
            archivo_limpio=limpio,
            archivo_observado=observado,
        )

        assert False, (
            "Se esperaba ValueError "
            "por columnas faltantes"
        )

    except ValueError as exc:
        mensaje = str(exc)

        assert (
            "Faltan columnas obligatorias"
            in mensaje
        )