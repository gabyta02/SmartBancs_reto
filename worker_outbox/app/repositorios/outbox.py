def reclamar_eventos_pendientes(
    cur,
    limite: int = 20,
):
    """
    Reclama eventos pendientes para este worker.

    Usa FOR UPDATE SKIP LOCKED para que varios workers
    puedan trabajar en paralelo sin tomar los mismos eventos.

    Los eventos quedan en estado PROCESANDO antes de liberar
    la transacción.
    """

    cur.execute(
        """
        WITH eventos AS (
            SELECT id_eventos
            FROM eventos_outbox
            WHERE estado = 'PENDIENTE'
            ORDER BY creado_en ASC
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE eventos_outbox AS eo
        SET estado = 'PROCESANDO'
        FROM eventos
        WHERE eo.id_eventos = eventos.id_eventos
        RETURNING
            eo.id_eventos,
            eo.transaccion_id,
            eo.tipo_evento,
            eo.carga_util,
            eo.estado,
            eo.intentos,
            eo.creado_en,
            eo.publicado_en
        """,
        (limite,),
    )

    columnas = (
        "id_eventos",
        "transaccion_id",
        "tipo_evento",
        "carga_util",
        "estado",
        "intentos",
        "creado_en",
        "publicado_en",
    )

    return [
        dict(zip(columnas, fila))
        for fila in cur.fetchall()
    ]


def marcar_evento_publicado(
    cur,
    id_eventos,
):
    cur.execute(
        """
        UPDATE eventos_outbox
        SET
            estado = 'PUBLICADO',
            publicado_en = CURRENT_TIMESTAMP
        WHERE id_eventos = %s
          AND estado = 'PROCESANDO'
        """,
        (str(id_eventos),),
    )


def devolver_evento_a_pendiente(
    cur,
    id_eventos,
):
    """
    Incrementa intentos y devuelve el evento a PENDIENTE
    para que pueda ser reintentado posteriormente.
    """

    cur.execute(
        """
        UPDATE eventos_outbox
        SET
            estado = 'PENDIENTE',
            intentos = intentos + 1
        WHERE id_eventos = %s
          AND estado = 'PROCESANDO'
        """,
        (str(id_eventos),),
    )


def marcar_evento_fallido(
    cur,
    id_eventos,
):
    cur.execute(
        """
        UPDATE eventos_outbox
        SET
            estado = 'FALLIDO',
            intentos = intentos + 1
        WHERE id_eventos = %s
          AND estado = 'PROCESANDO'
        """,
        (str(id_eventos),),
    )


def obtener_evento_por_id(
    cur,
    id_eventos,
):
    cur.execute(
        """
        SELECT
            id_eventos,
            transaccion_id,
            tipo_evento,
            carga_util,
            estado,
            intentos,
            creado_en,
            publicado_en
        FROM eventos_outbox
        WHERE id_eventos = %s
        """,
        (str(id_eventos),),
    )

    fila = cur.fetchone()

    if fila is None:
        return None

    columnas = (
        "id_eventos",
        "transaccion_id",
        "tipo_evento",
        "carga_util",
        "estado",
        "intentos",
        "creado_en",
        "publicado_en",
    )

    return dict(zip(columnas, fila))