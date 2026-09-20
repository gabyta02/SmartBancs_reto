-- SmartBancs 

--- Extensión utilizada para generacion de identificador unico universal

create extension if not exists pgcrypto;
create extension if not exists pg_stat_statements;  

-- Tipos

create type estados_cuenta as enum (
    'ACTIVA',
    'BLOQUEADA',
    'CERRADA'
);

create type estados_transaccion as enum (
    'PENDIENTE',
    'COMPLETADA',
    'RECHAZADA'
);

create type estados_outbox as enum (
    'PENDIENTE',
    'PUBLICADO',
    'PROCESANDO',
    'FALLIDO'
);

create type tipos_movimiento as enum (
    'DEBITO', 
    'CREDITO'
);


-- Tablas

create table cuentas (
    id_cuenta UUID primary key default gen_random_uuid(),
    numero_cuenta varchar(30) not null unique,
    nombre_titular varchar(150) not null,
    saldo numeric (18,2) not null default 0.00,
    divisa varchar(3) not null default 'USD',
    estado estados_cuenta not null default 'ACTIVA',
    creado_en timestamptz not null default current_timestamp,
    actualizado_en timestamptz not null default current_timestamp,
    
    constraint chk_cuentas_saldo_no_negativo
        check (saldo >= 0),

    constraint chk_cuentas_formato_divisa
        check (divisa ~ '^[A-Z]{3}$')
);

create table transacciones (
    id_transaccion UUID primary key default gen_random_uuid(),
    id_idempotencia varchar(100) not null,
    hash_solicitud  char(64) not null,
    cuenta_origen_id UUID not null,
    cuenta_destino_id UUID not null,
    monto numeric(18,2) not null,
    divisa varchar(3) not null default 'USD',
    estado estados_transaccion not null default 'PENDIENTE',
    razon_fallo varchar(255),
    creado_en timestamptz not null default current_timestamp,
    completado_en timestamptz,

    constraint uq_transacciones_idempotencia    
        unique (id_idempotencia),

    constraint fk_transacciones_cuenta_origen
        foreign key (cuenta_origen_id)
        references cuentas(id_cuenta),
    
    constraint fk_transacciones_cuenta_destino
        foreign key (cuenta_destino_id)
        references cuentas(id_cuenta),

    constraint ck_transacciones_monto_positivo
        check (monto > 0),
    
    constraint ck_transacciones_cuentas_diferentes
        check (cuenta_origen_id <> cuenta_destino_id),

    constraint ck_transacciones_formato_divisa
        check (divisa ~ '^[A-Z]{3}$')
    
);

create table movimientos (
    id_movimiento UUID primary key default gen_random_uuid(),
    transaccion_id UUID not null references transacciones(id_transaccion),
    cuenta_id UUID not null references cuentas(id_cuenta),
    tipo tipos_movimiento not null,
    monto numeric(18,2) not null,
    saldo_resultante numeric(18,2) not null,
    creado_en timestamptz not null default current_timestamp,

    constraint ck_movimientos_monto_positivo 
        check (monto > 0),
    
    constraint uq_movimientos_tx_tipo 
        unique (transaccion_id, tipo)
);

create table eventos_outbox (
    id_eventos UUID primary key default gen_random_uuid(),
    transaccion_id UUID not null,
    tipo_evento varchar(100) not null,
    carga_util jsonb not null,
    estado estados_outbox not null default 'PENDIENTE',
    intentos integer not null default 0,
    creado_en timestamptz not null default current_timestamp,
    publicado_en timestamptz,

    constraint fk_eventos_outbox_transaccion
        foreign key (transaccion_id)
        references transacciones(id_transaccion),

    constraint ck_eventos_outbox_intentos_no_negativos
        check (intentos >= 0)

);

create table analisis_ia (
    id_analisi UUID primary key default gen_random_uuid(),
    transaccion_id UUID not null unique,
    score numeric(4,2) not null,
    nivel varchar(20) not null,
    factores jsonb not null,
    recomen_user text not null,
    recomen_admin text not null,
    modelo varchar(100) not null,
    creado_en timestamptz not null default current_timestamp,

    constraint fk_analisis_ia_transaccion
        foreign key (transaccion_id)
        references transacciones(id_transaccion)
);

-- Indices

create index idx_transacciones_cuenta_origen  
    on transacciones(cuenta_origen_id, creado_en);

create index idx_transacciones_cuenta_destino 
    on transacciones(cuenta_destino_id, creado_en);

create index idx_movimientos_cuenta_fecha     
    on movimientos(cuenta_id, creado_en);

create index idx_eventos_outbox_pendientes    
    on eventos_outbox(creado_en) where estado = 'PENDIENTE';


-- Inmutabilidad del libro mayor ------------------------------------------

create or replace function fn_bloquear_modificacion()
returns trigger language plpgsql as $$
begin
    raise exception 'La tabla % es append-only: % no permitido', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end;
$$;

create trigger trg_movimientos_inmutables
before update or delete on movimientos
for each row execute function fn_bloquear_modificacion();

create or replace function fn_bloquear_delete_transaccion()
returns trigger
language plpgsql
as $$
begin
    raise exception
        'Las transacciones no pueden eliminarse'
        using errcode = 'insufficient_privilege';
end;
$$;


create trigger trg_transacciones_no_delete
before delete on transacciones
for each row
execute function fn_bloquear_delete_transaccion();


create or replace function fn_controlar_update_transaccion()
returns trigger
language plpgsql
as $$
begin

    -- Datos financieros originales: inmutables
    if old.id_transaccion is distinct from new.id_transaccion
       or old.id_idempotencia is distinct from new.id_idempotencia
       or old.hash_solicitud is distinct from new.hash_solicitud
       or old.cuenta_origen_id is distinct from new.cuenta_origen_id
       or old.cuenta_destino_id is distinct from new.cuenta_destino_id
       or old.monto is distinct from new.monto
       or old.divisa is distinct from new.divisa
       or old.creado_en is distinct from new.creado_en
    then
        raise exception
            'Los datos financieros originales de una transacción son inmutables'
            using errcode = 'insufficient_privilege';
    end if;


    -- COMPLETADA es estado final
    if old.estado = 'COMPLETADA'
       and new.estado is distinct from old.estado
    then
        raise exception
            'Una transacción COMPLETADA no puede cambiar de estado'
            using errcode = 'check_violation';
    end if;


    -- RECHAZADA es estado final
    if old.estado = 'RECHAZADA'
       and new.estado is distinct from old.estado
    then
        raise exception
            'Una transacción RECHAZADA no puede cambiar de estado'
            using errcode = 'check_violation';
    end if;


    -- Únicas transiciones válidas desde PENDIENTE
    if old.estado = 'PENDIENTE'
       and new.estado not in (
           'PENDIENTE',
           'COMPLETADA',
           'RECHAZADA'
       )
    then
        raise exception
            'Transición de estado no permitida'
            using errcode = 'check_violation';
    end if;


    -- Si termina correctamente debe tener fecha de finalización
    if new.estado = 'COMPLETADA'
       and new.completado_en is null
    then
        raise exception
            'Una transacción COMPLETADA debe tener completado_en'
            using errcode = 'check_violation';
    end if;


    return new;
end;
$$;


create trigger trg_transacciones_update_controlado
before update on transacciones
for each row
execute function fn_controlar_update_transaccion();

-- Timestamp de actualizacion en cuentas

create or replace function fn_actualizar_timestamp()
returns trigger language plpgsql as $$
begin
    new.actualizado_en = current_timestamp;
    return new;
end;
$$;

create trigger trg_cuentas_actualizado_en
before update on cuentas
for each row execute function fn_actualizar_timestamp();

-- Permisos del usurario del microservicio

-- permite la conexion a la base de datos
grant connect 
on database smartbanks_db
to svc_banc_user;

-- permite acceso al esquema
grant usage 
on schema public
to svc_banc_user;

-- permisos sobre las tablas
grant select, insert, update
on all tables in schema public
to svc_banc_user;

-- permisos para tablas futuras
alter default privileges 
in schema public
grant select, insert, update
on tables
to svc_banc_user;


-- Limites de tiempo a nivel de rol: aplican a toda conexion del servicio,
alter role svc_banc_user set lock_timeout = '800ms';
alter role svc_banc_user set statement_timeout = '1500ms';
alter role svc_banc_user set idle_in_transaction_session_timeout = '5s';