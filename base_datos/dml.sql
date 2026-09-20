-- SmartBancs
-- Datos adicionales para desarrollo y pruebas

insert into cuentas (
    numero_cuenta,
    nombre_titular,
    saldo,
    divisa,
    estado
)
values
    -- Cuentas activas con saldos variados
    ('SB-000006', 'María Fernanda Torres',   2450.75, 'USD', 'ACTIVA'),
    ('SB-000007', 'Carlos Andrés Paredes',    120.40, 'USD', 'ACTIVA'),
    ('SB-000008', 'Lucía Gabriela Moreno',   9875.10, 'USD', 'ACTIVA'),
    ('SB-000009', 'José Luis Cevallos',      4300.00, 'USD', 'ACTIVA'),
    ('SB-000010', 'Ana Belén Villacís',       675.25, 'USD', 'ACTIVA'),
    ('SB-000011', 'Pedro Nicolás Guamán',    1890.90, 'USD', 'ACTIVA'),
    ('SB-000012', 'Daniela Estefanía Núñez', 3320.00, 'USD', 'ACTIVA'),
    ('SB-000013', 'Miguel Ángel Chávez',      15.00,  'USD', 'ACTIVA'),
    ('SB-000014', 'Sofía Alejandra Ortiz',   5210.60, 'USD', 'ACTIVA'),
    ('SB-000015', 'Andrés Sebastián Lema',   1000.00, 'USD', 'ACTIVA'),

    -- Casos límite de saldo
    ('SB-000016', 'Usuario Saldo Mínimo',       0.01, 'USD', 'ACTIVA'),
    ('SB-000017', 'Usuario Saldo Alto',    250000.00, 'USD', 'ACTIVA'),
    ('SB-000018', 'Usuario Saldo Redondo',    100.00, 'USD', 'ACTIVA'),

    -- Cuentas en otros estados
    ('SB-000019', 'Usuario Demo Cerrado',       0.00, 'USD', 'CERRADA'),
    ('SB-000020', 'Usuario Demo Bloqueado 2', 780.50, 'USD', 'BLOQUEADA'),

    -- Cuentas activas con saldos variados
    ('SB-000021', 'Valeria Monserrat Andrade',  2100.00, 'USD', 'ACTIVA'),
    ('SB-000022', 'Kevin Joel Salazar',          450.30, 'USD', 'ACTIVA'),
    ('SB-000023', 'Gabriela Nicole Pazmiño',    3875.90, 'USD', 'ACTIVA'),
    ('SB-000024', 'Diego Armando Yánez',        1250.00, 'USD', 'ACTIVA'),
    ('SB-000025', 'Camila Anahí Rivera',         980.45, 'USD', 'ACTIVA'),
    ('SB-000026', 'Fernando Xavier Ayala',      6400.00, 'USD', 'ACTIVA'),
    ('SB-000027', 'Paola Alejandra Guerrero',    215.75, 'USD', 'ACTIVA'),
    ('SB-000028', 'Jorge Iván Mendoza',         5000.00, 'USD', 'ACTIVA'),
    ('SB-000029', 'Karla Denise Bonilla',       1720.20, 'USD', 'ACTIVA'),
    ('SB-000030', 'Ricardo Emilio Cárdenas',    8350.00, 'USD', 'ACTIVA');