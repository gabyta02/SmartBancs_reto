INSERT INTO cuentas (
    numero_cuenta,
    nombre_titular,
    saldo,
    divisa,
    estado
)
SELECT
    'LOAD-' || LPAD(i::text, 6, '0'),
    'Usuario Carga ' || i,
    100000.00,
    'USD',
    'ACTIVA'
FROM generate_series(1, 5000) AS g(i)
ON CONFLICT (numero_cuenta) DO NOTHING;