# Plan de Escalabilidad Desktop para AMDockVS

Fecha: `2026-06-07`

## Objetivo

Rediseñar la arquitectura de datos e ingestión de `AMDockVS` para soportar millones de moléculas en un entorno desktop con recursos limitados, manteniendo:

- uso de memoria acotado
- costo de ingestión por lote estable
- tamaño de `project.db` controlado
- rutas de query rápidas sobre un subconjunto pequeño y explícito de campos

La meta no es conservar toda la riqueza del dominio en forma totalmente relacional. La meta es separar claramente:

- datos calientes para query
- metadata fría para inspección
- artefactos pesados para almacenamiento externo

## Alcance

Este plan cubre `AMDockVS` como app química construida sobre `MolSuite`.

Cubre:

- modelo de persistencia en `project.db`
- pipeline de importación
- materialización de ligandos y receptores
- estrategia de metadata/artifacts
- contrato de query y selección

No cubre rediseños amplios de UI salvo donde dependan de la forma de los datos.

## Restricciones de operación

`AMDockVS` seguirá funcionando como una app local sobre un `MolSuite` single-app e in-process.

Esto implica:

- un único dueño operativo del proyecto
- ausencia de multiusuario concurrente
- posibilidad futura de backend local `PostgreSQL` sin convertir la app en servicio compartido

El objetivo no es servir múltiples clientes. El objetivo es escalar mejor dentro de una sola sesión de trabajo local.

## Diagnóstico resumido

El estado actual muestra varios problemas de escalabilidad de dominio:

1. Cada molécula puede explotar en muchas rows relacionales de bajo valor operativo, especialmente en `molecule_source_properties`.
2. La importación hace demasiado IO por molécula:
   - escribir molécula original
   - fragmentar
   - escribir fragmentos
   - releer fragmento seleccionado
   - recalcular filtros sobre esa lectura
3. El pipeline mezcla ingestión, normalización, fragmentación, enriquecimiento y persistencia final en una sola ruta pesada.
4. Se están modelando como tablas detalles que no son parte del path principal de query.
5. El crecimiento del dataset amenaza tanto el tamaño de `project.db` como el costo del import.
6. El flujo actual es demasiado molecule-céntrico para escenarios de millones donde la mayoría de entidades terminarán descartadas y nunca se inspeccionarán individualmente.

La consecuencia es clara: si se mantiene el diseño actual, llegar a millones de registros degradará:

- tiempo de ingestión
- tamaño de DB
- mantenimiento de índices
- rendimiento de backup/migración

## Principios rectores

### 1. Query hot path pequeño y explícito

Solo un subconjunto reducido de datos debe vivir como columnas indexables.

Ese subconjunto debe cubrir:

- selección de ligandos/receptores
- filtros fisicoquímicos frecuentes
- estado operacional del dataset
- joins de primer orden realmente necesarios

### 2. Metadata fría compactada

La metadata que no participa en queries frecuentes debe compactarse en:

- JSON en una columna
- artifact sidecar en archivo

y debe evitarse modelarla como muchas rows relacionales.

### 3. Artefactos grandes fuera de la DB principal

No conviene usar `project.db` como contenedor de todo el detalle químico o de importación. La DB debe apuntar a artifacts, no absorberlos indiscriminadamente.

### 4. Ingestión por etapas

Importar no debe significar "hacer todo". Debe separarse:

1. captura mínima
2. persistencia base
3. enriquecimiento diferido

### 5. Desktop-first

El costo del sistema debe depender del lote activo y no del volumen histórico completo del proyecto.

### 6. `SQLite` no se descarta, pero se usa con disciplina

Si el esquema y el pipeline son compactos, `SQLite` puede seguir siendo útil. Si el diseño sigue explotando una molécula en decenas de filas frías, ningún backend arreglará el problema de fondo.

### 7. Dos flujos explícitos

`AMDockVS` no debe forzar un solo modelo de trabajo.

Debe soportar dos flujos distintos:

- flujo materializado y molecule-céntrico
- flujo masivo, operation-céntrico y de materialización diferida

El primero optimiza trazabilidad e inspección. El segundo optimiza descarte temprano y throughput.

## Arquitectura objetivo

### A. Tabla principal compacta de moléculas

La tabla principal debe conservar solo:

- identidad y tipo
- paths principales
- estado operativo
- columnas fisicoquímicas calientes
- flags de exclusión/uso
- contexto primario
- referencias a artifacts

### B. Metadata fría consolidada

`source_properties` debe dejar de ser una tabla fila-a-fila para el caso general.

Debe consolidarse en una de estas formas:

1. JSON por molécula
2. JSON por import record
3. artifact externo con referencia desde la molécula

La recomendación principal es:

- JSON compacto por molécula para metadata mediana
- artifact externo para payloads grandes o heterogéneos

En el flujo materializado, un JSON por molécula es aceptable si se almacena en un esquema shardeado de artifacts y no en una carpeta por molécula.

La prioridad es eficiencia, no agrupación manual del contenido en disco.

### C. Enriquecimiento diferido

La fragmentación, selección de fragmento y cálculos derivados no deben bloquear toda la persistencia base del import si no son imprescindibles para que la molécula exista en el catálogo.

### D. Contrato de query estricto

Las queries normales de catálogo no deben depender de:

- inspeccionar JSON arbitrario
- expandir tablas enormes de propiedades frías
- rehidratar artifacts para filtrar

### E. Artifacts como primer ciudadano

`AMDockVS` ya maneja archivos moleculares; debe abrazar un modelo donde:

- la DB indexa
- los archivos almacenan detalle pesado
- la metadata fría solo conserva una referencia compacta

La organización física de artifacts debe ser:

- estable
- shardeada
- apta para millones de archivos

y no debe asumir navegación manual por carpetas como operación normal del usuario.

### F. Flujo dual de datos

#### Flujo 1. Materializado

Pensado para:

- miles o decenas de miles
- exploración interactiva
- inspección individual
- trazabilidad rica

Aquí cada molécula puede tener representación completa y metadata fría propia.

#### Flujo 2. Masivo y diferido

Pensado para:

- cientos de miles o millones
- filtros tempranos
- scoring o descarte a gran escala
- promoción posterior de subconjuntos

Aquí el sistema debe poder operar sin materializar cada molécula como entidad rica desde el inicio.

La promoción posterior debe permitir que el usuario diga:

- "hasta aquí operé a gran escala"
- "a partir de este subconjunto sí quiero materialización completa"

## Datos calientes y fríos

### Datos calientes recomendados

Deben permanecer como columnas utilizables para query:

- `is_ligand`
- `is_receptor`
- `molecule_type`
- `usage_class`
- `primary_context`
- `excluded`
- `in_set`
- `n_atoms`
- `mw`
- `exact_mw`
- `logp`
- `hbd`
- `hba`
- `tpsa`
- `rotatable_bonds`
- `fragment_count`
- `ring_count`
- `aromatic_ring_count`
- `hetero_atom_count`
- `heavy_atom_count`
- `formal_charge`
- `fraction_csp3`
- `has_3d`
- `has_hs`
- `is_minimized`
- `has_activity`

### Datos fríos recomendados

Deben migrar a JSON o artifact:

- propiedades de origen crudas
- detalle completo de fragmentación
- metadata rica de importación
- campos opcionales específicos de un formato
- observaciones de parser/procesamiento

En flujo masivo, incluso parte de esta metadata puede no materializarse por molécula salvo que la entidad sea promovida.

## Decisiones de arquitectura que deben cerrarse

1. `molecule_source_properties` deja de ser la representación principal de metadata de importación.
2. El catálogo usará un contrato de query centrado en columnas calientes.
3. La importación pasará a un esquema multi-etapa.
4. La fragmentación y otros enriquecimientos pesados podrán diferirse.
5. `project.db` no debe absorber payloads grandes cuando un artifact externo es suficiente.
6. Debe existir un modo masivo donde no se materialice cada molécula completa desde el inicio.
7. Las operaciones críticas sobre `project.db` deben pasar por la API de `MolSuite` y una capa basada en `SQLModel`, no por accesos específicos a `SQLite`.

## Fase 0 - Contrato de datos y consultas

### Objetivo

Definir qué se consulta de verdad y qué solo se conserva por trazabilidad.

### Trabajo

1. Inventariar filtros y queries reales del catálogo.
2. Clasificar campos en:
   - hot
   - warm
   - cold
3. Formalizar qué columnas deben seguir indexadas.
4. Formalizar qué metadata queda explícitamente fuera del path de query.
5. Separar explícitamente el contrato de datos para:
   - flujo materializado
   - flujo masivo diferido

### Criterio de salida

Existe un contrato de query que justifica el esquema compacto.

## Fase 1 - Compactación del esquema de moléculas

### Objetivo

Reducir el fan-out por molécula en `project.db`.

### Trabajo

1. Rediseñar la persistencia de `source_properties`.
2. Introducir un campo consolidado de metadata fría por molécula o por import record.
3. Definir storage externo shardeado para metadata/artifacts por molécula.
4. Revisar si `molecule_models` debe mantenerse separado siempre o solo cuando existe multiplicidad real de modelos.
5. Minimizar índices durante ingestión masiva y revisar cuáles son realmente necesarios.
6. Diseñar el esquema nuevo de forma paralela al actual para facilitar integración progresiva y rescatar la mayor parte del flujo existente.

### Criterio de salida

El número de rows generadas por molécula deja de crecer con la cantidad de propiedades frías.

## Fase 2 - Rediseño del pipeline de importación

### Objetivo

Separar importación base de enriquecimiento pesado.

### Trabajo

1. Dividir el pipeline en:
   - parse/import base
   - persistencia mínima
   - enriquecimiento opcional o diferido
2. Asegurar que una molécula pueda entrar al catálogo sin requerir toda la cadena de fragmentación/derivación.
3. Reducir el número de escrituras a disco por molécula durante el camino base.
4. Definir explícitamente el flujo masivo donde la entidad completa solo se materializa tras promoción.

### Criterio de salida

El tiempo de ingestión base deja de depender de toda la química derivada opcional.

## Fase 3 - Eliminación de IO redundante por molécula

### Objetivo

Reducir el costo fijo del materializador.

### Trabajo

1. Evitar secuencias del tipo:
   - escribir
   - releer
   - recalcular
2. Mantener el objeto/mol/estado en memoria dentro del lote activo cuando sea viable.
3. Revisar si el fragmento seleccionado puede resolverse sin round-trip a archivo.
4. Mover a artifacts solo lo que deba sobrevivir al lote, no lo que solo se necesita para decidir.
5. Permitir que en flujo masivo parte de este costo ni siquiera se ejecute hasta la promoción.

### Criterio de salida

El costo de IO por molécula durante importación disminuye de forma sustancial.

## Fase 4 - Enriquecimiento diferido y perfiles de importación

### Objetivo

Permitir imports rápidos y imports ricos sin forzar el peor caso siempre.

### Trabajo

1. Definir perfiles de importación:
   - mínimo
   - estándar
   - enriquecido
   - masivo diferido
2. Permitir que fragmentación, descriptorización o filtros ricos corran como jobs posteriores.
3. Marcar explícitamente la disponibilidad de capacidades derivadas por molécula o por lote promovible.

### Criterio de salida

El usuario puede elegir entre throughput alto y enriquecimiento profundo sin cambiar de arquitectura.

## Fase 5 - Estrategia de artifacts y metadata

### Objetivo

Poner límites claros entre DB y filesystem.

### Trabajo

1. Definir qué vive en:
   - columna
   - JSON compacto
   - artifact sidecar
2. Asegurar que la recuperación de artifacts sea trazable y estable.
3. Evitar duplicación innecesaria entre:
   - paths
   - metadata JSON
   - tablas auxiliares
4. Definir un layout shardeado para evitar directorios gigantes y permitir millones de artifacts.
5. Asegurar que la colocalización lógica por molécula no dependa de estructura manual de carpetas, sino de la API.

### Criterio de salida

`project.db` queda como índice operativo del proyecto, no como contenedor indiscriminado de todo el detalle químico.

## Fase 6 - Estrategia de backend

### Objetivo

Decidir racionalmente hasta dónde llevar `SQLite` y cuándo abrir la puerta a otro backend.

### Trabajo

1. Medir el esquema compacto bajo volumen alto.
2. Establecer límites observables para:
   - tamaño aceptable de `project.db`
   - tiempos de insert por lote
   - tiempos de query del catálogo
3. Desacoplar la capa crítica de `project.db` de `SQLite` usando la API de `MolSuite` y una base `SQLModel`/SQLAlchemy portable.
4. Solo después evaluar si hace falta:
   - backend alterno
   - estrategia híbrida
   - separación adicional de stores

### Criterio de salida

La decisión sobre `SQLite` se toma con benchmarks del diseño corregido, no con síntomas del diseño actual, y `project.db` deja de estar estructuralmente atada a rutas SQLite-específicas.

## Benchmarks obligatorios

Antes de cerrar esta línea deben existir benchmarks al menos para:

1. ingestión de 100k, 500k y 1M moléculas
2. tamaño de `project.db` en esquema actual vs esquema compacto
3. tiempo de import base vs import enriquecido
4. tiempo de flujo materializado vs flujo masivo diferido
5. latencia de queries de catálogo sobre campos calientes
6. consumo máximo de memoria durante ingestión prolongada

## Criterios de salida globales

`AMDockVS` puede considerarse bien encaminado para millones de moléculas en desktop cuando:

1. la cantidad de rows por molécula es esencialmente constante y baja
2. la metadata fría no domina el tamaño de `project.db`
3. el import base no obliga a ejecutar todo el enriquecimiento químico
4. el catálogo consulta columnas calientes y no depende de JSON ni tablas frías
5. los artifacts pesados viven fuera de la DB principal
6. existe un flujo masivo que puede operar sin materialización completa por molécula
7. el consumo de memoria y procesos se mantiene acotado por lote

## Riesgos a vigilar

1. Compactar en JSON y luego volver a usar ese JSON para query frecuente.
2. Mover demasiado pronto datos relacionales útiles a artifacts opacos.
3. Romper compatibilidad con vistas o widgets que hoy asumen tablas auxiliares detalladas.
4. Mantener el pipeline monolítico y solo cambiar el esquema, dejando intacto el costo químico/IO.
5. Diseñar el flujo masivo sin un mecanismo claro de promoción posterior hacia el flujo materializado.

## Resultado esperado

`AMDockVS` queda con una arquitectura de datos apta para desktop masivo:

- catálogo compacto
- metadata fría consolidada
- storage shardeado de metadata/artifacts
- ingestión por etapas
- flujo dual: materializado y masivo diferido
- artifacts pesados fuera de la DB principal
- crecimiento sostenible hacia millones de moléculas
