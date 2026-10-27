# Plan De Refactor De Importacion

## Alcance

Este documento define el plan de refactor para `/src/amdock/io` a partir de la implementacion actual y de las siguientes restricciones:

- AMDock no debe escribir datos del proyecto mediante logica ad hoc directa sobre la base de datos si MolSuite ya ofrece una via delegada adecuada.
- El codigo de importacion debe separar:
  - parseo de archivos e instanciacion molecular
  - decisiones de dominio
  - persistencia
  - orquestacion de jobs y feeds
- El progreso de importacion debe seguir siendo responsivo incluso con fuentes muy grandes.
- El estado molecular debe derivarse de objetos moleculares reales, no de convenciones por extension o escaneos de texto simplificados.

## Problemas Actuales

1. La resolucion de rutas y recursos de almacenamiento esta duplicada en varios jobs y repositorios.
2. Los jobs de importacion se solapan mucho y solo estan parcialmente especializados.
3. `count_import_records()` es bloqueante y escala mal en archivos grandes.
4. Helpers de normalizacion como `_normalize_molecule_kind()` ocultan errores del llamador en lugar de exigir una intencion explicita de importacion.
5. Helpers de quimica viven en `io/loaders.py` en lugar de una capa de quimica o parsing.
6. `_count_atoms_from_structure_lines()` no es una estrategia valida para obtener informacion molecular.
7. El flujo de prefiltrado de ligandos esta sobrecomplicado y sobreanotado para un camino que descarta temprano.
8. La carga de SDF/SMILES duplica logica que ya existe en los suppliers adecuados.
9. La carga de estructuras unitarias es demasiado superficial y depende de supuestos por formato.
10. Los helpers de streaming reimplementan comportamiento que deberia venir de readers o suppliers reales.
11. La persistencia se hace hoy manualmente dentro de AMDock en lugar de pasar por un contrato delegado de MolSuite.

## Arquitectura Objetivo

La pila de importacion debe dividirse en cinco capas:

1. `io/api.py`
   - puntos de entrada publicos de importacion
   - validacion de la intencion explicita del llamador
   - construccion de objetos de request de importacion

2. `io/jobs.py`
   - definicion de jobs homogeneos de MolSuite
   - orquestacion de feeds, chunks y progreso
   - no debe contener el detalle del parsing

3. `io/parsers/`
   - readers especificos por formato
   - devuelven objetos moleculares y payloads normalizados
   - sin acceso directo a base de datos

4. `io/transformers/`
   - convierten la salida del parser en filas de dominio
   - derivan estado comun (`has_3d`, `has_hs`, numero de atomos, representaciones)
   - aplican prefilters tempranos opcionales

5. capa de persistencia delegada
   - escribe filas relacionadas (`molecules`, `ligands`, `receptors`, tablas especificas por tipo)
   - usa la infraestructura de escritura de MolSuite
   - evita `flush()/refresh()` por fila dentro de AMDock

## Decisiones De Diseno

### 1. Unificar la resolucion de recursos y rutas de almacenamiento

Debe existir un modulo compartido unico, preferiblemente primero dentro de AMDock:

- `amdock/io/resources.py` o `amdock/io/paths.py`

Responsabilidades:

- resolver la raiz de almacenamiento molecular
- resolver subdirectorios por rol
- resolver rutas gestionadas derivadas del archivo fuente
- exponer una API canonica usada por importacion, quimica y docking

Extraccion futura a MolSuite solo si:

- la logica pasa a ser realmente agnostica de la aplicacion
- varias apps necesitan exactamente la misma semantica de recursos

Hoy sigue siendo especifica de AMDock.

### 2. Mantener jobs homogeneos, reduciendo duplicacion

La multiplicidad actual de jobs es aceptable si se vuelven claramente especializados:

- `load_ligands_*`
- `load_receptors_*`
- `load_molecules_*`

Pero deben compartir primitivas internas comunes:

- enumeracion de archivos
- emision de chunks
- despacho a parsers
- persistencia delegada

La capa de jobs debe diferenciarse principalmente por:

- rol explicito
- restricciones explicitas de `molecule_kind`
- formatos permitidos
- soporte opcional de prefilter

### 3. Hacer el conteo de registros asincrono y no bloqueante para la planificacion

`count_import_records()` no debe bloquear la creacion del job ni congelar la UI.

Plan:

- eliminar el conteo como precondicion dura para crear el job
- enviar los jobs de importacion inmediatamente
- calcular totales en paralelo de forma asincrona cuando el formato lo permita
- actualizar monitor y progreso cuando el total ya sea conocido

Comportamiento esperado:

- antes de conocer el total: progreso indeterminado o abierto
- despues de conocer el total: conversion a progreso acotado

Politica de conteo por formato:

- SDF: escaneo asincrono solo cuando haga falta
- SMILES: conteo asincrono de lineas solo cuando haga falta
- archivos de estructura unica: conteo trivial igual a 1

La importacion de archivos grandes debe arrancar rapido incluso si el total final tarda en calcularse.

### 4. Eliminar normalizaciones implicitas

Helpers como `_normalize_molecule_kind()` deben eliminarse o reducirse a validacion estricta.

Reglas:

- la intencion de importacion debe ser explicita en el borde del API
- combinaciones invalidas de rol y kind deben fallar temprano
- no debe haber fallbacks silenciosos desde entradas invalidas del llamador hacia valores inferidos internamente

Los valores permitidos deben definirse centralmente y validarse una sola vez.

### 5. Mover helpers quimicos fuera de `io/loaders.py`

Funciones como:

- `_mol_has_3d`
- `_mol_has_explicit_hs`

pertenecen a un modulo de estado o de inspeccion quimica, por ejemplo:

- `amdock/chemistry/state.py`
- `amdock/chemistry/introspection.py`

Estos helpers deben operar sobre objetos moleculares y ser reutilizables fuera de la importacion.

### 6. Eliminar el conteo de atomos por escaneo de texto

`_count_atoms_from_structure_lines()` debe eliminarse.

El numero de atomos debe salir de un objeto molecular parseado:

- RDKit para formatos compatibles con small molecules
- un parser adecuado para biopolimeros o macromoleculas cuando corresponda

No debe inferirse el numero de atomos a partir de escaneos de texto `ATOM/HETATM` como camino canonico.

### 7. Simplificar el flujo de prefilter

La semantica actual de descarte temprano es correcta:

- si una molecula no pasa el prefilter, no se persiste

Por tanto:

- no se necesita una canalizacion pesada de anotacion para los descartados
- la anotacion solo tiene sentido si se reutiliza para filas que si se persisten

Plan:

- mantener el prefilter como una compuerta pura de aceptacion o rechazo
- conservar solo la generacion minima reutilizable de reportes o descriptores para las filas aceptadas
- evitar decoracion de metadata para filas que nunca se escribiran

### 8. Reemplazar la iteracion custom de SDF por readers basados en suppliers

Para importacion de SDF:

- usar `SDMolSupplier` para parsing lazy monohilo cuando importe la contabilidad exacta
- usar `MultithreadedSDMolSupplier` cuando importe mas el throughput que el preconteo exacto

Regla de implementacion:

- la seleccion del parser debe ser explicita
- no debe mantenerse logica paralela propia de parseo por bloques salvo para edge cases reales no soportados

### 9. Reemplazar la iteracion custom de SMILES por readers basados en suppliers

Para importacion de SMILES:

- usar `SmilesMolSupplier`
- usar `MultithreadedSmilesMolSupplier` cuando corresponda

Esto elimina tokenizacion duplicada y divergencia frente al comportamiento real de RDKit.

### 10. Reconstruir la importacion de estructura unica alrededor de instancias moleculares reales

`_materialize_single_structure_rows()` debe convertirse en un punto de despacho por formato, no en un copiador superficial.

Comportamiento requerido por formato:

- `mol2`
  - parsear estructura real
  - computar numero de atomos
  - detectar 3D desde coordenadas
  - detectar hidrogenos explicitos desde atomos reales

- `pdb`
  - parsear modelos o cadenas segun corresponda
  - determinar si existen multiples modelos
  - definir regla de importacion para seleccion de modelo
  - computar hidrogenos y 3D desde la estructura parseada

- `pdbqt`
  - parsear como estructura consciente de preparacion
  - determinar hidrogenos, 3D y estado de preparacion con cuidado, no por extension

- otros formatos futuros
  - conectarlos mediante un registro de parsers

Ningun formato debe implicar estado por el simple hecho de su sufijo.

### 11. Eliminar helpers de streaming custom si readers o suppliers ya resuelven el problema

`_stream_sdf_entries()` y `_stream_smiles_entries()` deben desaparecer si la capa de parsers expone:

- iteracion lazy de registros
- conteo opcional de registros
- indexacion de origen consistente
- manejo de errores y reporte

Si aun hace falta un wrapper adicional, debe ser fino y agnostico del parser.

## Plan De Persistencia

La implementacion actual de `persist_molecule_import_rows()` debe sustituirse por persistencia delegada mediante MolSuite.

Modelo objetivo de escritura:

- nodo: `molecules`
- nodo: `ligands`
- nodo: `receptors`
- nodos futuros opcionales:
  - `small_molecule_data`
  - `protein_data`
  - `representations`

Requisitos:

- escritura bulk
- resolucion de relaciones por referencias, no por `flush` ORM fila a fila
- un contrato de escritura por batch de importacion
- sin ciclo de vida manual de `ProjectDB()` dentro del codigo de importacion de AMDock

El mecanismo deseado es la escritura relacional tipo grafo de MolSuite, no una sesion manual ad hoc en AMDock.

## Estructura De Modulos Propuesta

Estructura sugerida:

```text
src/amdock/io/
  api.py
  jobs.py
  paths.py
  requests.py
  persistence.py
  parsers/
    __init__.py
    base.py
    sdf.py
    smiles.py
    single_structure.py
    registry.py
  transformers/
    __init__.py
    molecules.py
    filters.py
    representations.py
```

Y helpers de estado quimico fuera de importacion:

```text
src/amdock/chemistry/
  state.py
  introspection.py
```

## Fases De Ejecucion

### Fase 1. Estabilizar contratos

- definir modelo explicito de request de importacion
- definir validacion estricta de rol y kind
- centralizar resolucion de recursos y rutas

### Fase 2. Extraer capa de parsers

- agregar reader SDF basado en `SDMolSupplier`
- agregar reader SMILES basado en `SmilesMolSupplier`
- agregar despacho de parser para estructuras unitarias

### Fase 3. Extraer derivacion de estado molecular

- computar `has_3d`, `has_hs`, numero de atomos y representaciones 1D desde objetos parseados
- mover helpers quimicos fuera de `io/loaders.py`

### Fase 4. Simplificar prefilter

- convertir el prefilter en una compuerta acotada de aceptacion o rechazo
- eliminar rutas de anotacion innecesarias para filas descartadas

### Fase 5. Reemplazar persistencia directa

- eliminar escrituras manuales con `ProjectDB()` desde la importacion de AMDock
- cambiar a persistencia delegada tipo grafo en MolSuite

### Fase 6. Rehacer el modelo de progreso

- desacoplar el lanzamiento del job del preconteo
- agregar reporte asincrono del conteo
- soportar transicion de progreso indeterminado a determinado

### Fase 7. Colapsar helpers y jobs redundantes

- borrar helpers obsoletos de normalizacion y streaming
- reducir codigo duplicado de los jobs de importacion a logica de orquestacion

## Criterios De Aceptacion

El refactor se considera completo cuando:

1. El envio de jobs de importacion es rapido incluso para archivos muy grandes.
2. No queda gestion ad hoc directa de sesion de DB en la persistencia de importacion de AMDock.
3. El estado molecular se deriva de objetos parseados, no de extensiones o escaneos de texto.
4. La importacion de SDF/SMILES usa suppliers reales en lugar de iteracion custom por lineas o bloques.
5. La validacion de rol y kind es explicita y estricta.
6. `loaders.py` queda reducido a orquestacion o desaparece en favor de modulos mas claros de parser y transformacion.
7. La importacion de estructuras unitarias produce el mismo nivel de calidad de estado que SDF/SMILES.
8. El progreso de importacion puede arrancar inmediatamente sin esperar el conteo completo de registros.

## Notas

- El problema del conteo es un problema del modelo de progreso, no una razon para seguir bloqueando el inicio del job.
- El problema de persistencia es de arquitectura, no solo de rendimiento.
- El codigo actual funciona para casos moderados, pero no escala limpiamente a importaciones muy grandes ni a soporte mas amplio de tipos moleculares sin este refactor.
