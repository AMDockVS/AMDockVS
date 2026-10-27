# Plan de ejecución remota: responsabilidades de `ms_flow` y AMDockVS

Fecha: 2026-09-03  
Estado: propuesta revisada contra el código actual.

Este documento define cómo AMDockVS debe ejecutar trabajo en máquinas locales mediante Ray y
en HPC mediante SSH y un scheduler. Separa explícitamente la infraestructura genérica que
pertenece a `ms_flow` del dominio químico y los perfiles de instalación que pertenecen a
AMDockVS.

Complementa:

- `plan_modos_vs_htpvs_2026-09-03.md`, que define los modos `vs` y `htpvs`, los shards y el
  punto de promoción.
- `plan_normalizacion_y_htp_2026-08-29.md`, que define la base del almacenamiento HTP.

No modifica la decisión fundamental del modo HTP: durante la campaña los ligandos se manejan
como shards y agregados; únicamente los hits promovidos se materializan como moléculas normales
de AMDockVS.

---

## Resumen ejecutivo

El soporte actual no parte de cero:

| Escenario | Estado actual |
|---|---|
| Ray LAN sin carpeta compartida | El arranque de Ray y la transferencia de archivos mediante objetos Ray ya existen |
| Ray LAN con carpeta compartida | Existe `shared_fs`, pero no se instala, monta ni valida SSHFS |
| HPC mediante SSH y scheduler | Existe el contrato genérico submit/poll/cancel y estado por chunk, pero no el transporte remoto completo |
| Ray multinodo dentro de una asignación HPC | No está implementado |

La dirección propuesta es correcta, pero debe mantenerse esta frontera:

> `ms_flow` posee conexiones, transporte, aprovisionamiento, ejecutores, persistencia operativa,
> polling y limpieza. AMDockVS posee pipelines químicos, shards, perfiles de software químico,
> selección de hits y presentación de resultados.

---

# Parte I — `ms_flow`

## 1. Responsabilidad de `ms_flow`

`ms_flow` debe ofrecer una API genérica para:

1. Describir y comprobar hosts remotos.
2. Ejecutar comandos por SSH sin interacción.
3. Comprobar o reconstruir entornos de ejecución según una política explícita.
4. Iniciar y detener clusters Ray administrados.
5. Transportar entradas y resultados cuando no existe un filesystem compartido.
6. Montar y validar opcionalmente una carpeta SSHFS en una LAN.
7. Preparar, enviar, consultar, recuperar y cancelar campañas de un scheduler HPC.
8. Persistir suficiente información para recuperar una campaña después de reiniciar la
   aplicación.
9. Limpiar datos temporales sin eliminar resultados que aún no hayan sido recuperados.

No debe conocer ligandos, receptores, motores de docking, fingerprints, descriptores ni criterios
de selección química.

## 2. Lo que ya existe

### 2.1 Aprovisionamiento y Ray

`core/executor/provisioning.py` ya proporciona:

- SSH mediante el cliente del sistema.
- `BatchMode=yes`, timeout y comprobación de host.
- pruebas de conectividad y `ray --version`.
- generación de configuración de cluster Ray.
- comandos `ray up` y `ray down`.

Esto debe ampliarse; no debe crearse un segundo subsistema de aprovisionamiento.

### 2.2 Transporte Ray

`core/data/planner.py`, `core/data/bridge.py` y `core/executor/ray_adapter.py` ya distinguen:

- ruta compartida cuando `shared_fs=True`;
- transporte mediante Ray cuando no hay filesystem compartido;
- entrega por valor para objetos pequeños;
- entradas y directorios de salida declarados mediante los contratos de datos de `ms_flow`.

Los temporales normales de cada tarea se eliminan, pero el caché remoto de archivos en
`/tmp/ms_flow_ray_cache` permanece durante la vida del nodo y actualmente no tiene TTL ni límite
de tamaño.

### 2.3 HPC

`core/executor/hpc_adapter.py` y `core/executor/hpc_runtime.py` ya implementan una base genérica:

- `submit`, `poll` y `cancel` mediante comandos configurables;
- `manifest.json`, `status.json`, `result.json` y `submit.sh` por chunk;
- escritura atómica del estado de la ejecución;
- ejecución de un callable serializado.

Esto todavía presupone que los archivos son visibles localmente. El staging actual usa copia
local y no SSH/rsync. Tampoco se levanta Ray dentro de los nodos asignados.

## 3. Configuración SSH común

Debe existir una sola configuración de host remoto, reutilizada por Ray LAN y HPC. El modelo
`SSHHost` existente debe integrarse o evolucionar, no duplicarse.

Campos mínimos:

```text
address
user
port
identity_file
working_directory
conda_executable
conda_environment
environment_policy
connect_timeout
```

`environment_policy` inicialmente sólo necesita dos valores:

- `require`: política predeterminada. Si el entorno no existe o no funciona, se informa el
  fallo y no se modifica el host.
- `recreate`: elimina y reconstruye explícitamente el entorno.

No se deben almacenar contraseñas. La conexión debe exigir claves SSH funcionales y conservar
`BatchMode=yes` para evitar bloqueos esperando entrada interactiva.

### 3.1 Secuencia de preparación

Para cada host:

1. Comprobar la conexión SSH.
2. Crear el `working_directory` si no existe y comprobar que es escribible.
3. Resolver el ejecutable Conda o Micromamba configurado.
4. Comprobar el entorno con `conda run -n <env> python -V`.
5. Fallar si no existe y la política es `require`.
6. Reconstruirlo únicamente si la política es `recreate`.
7. Instalar el wheel y los extras solicitados por la aplicación.
8. Comprobar imports y ejecutables declarados por el perfil de aplicación.
9. Ejecutar una tarea mínima real en el worker.

`ms_flow` gestiona esta secuencia, pero AMDockVS proporciona el perfil concreto de paquetes,
imports y programas que debe comprobarse.

## 4. Máquinas LAN con Ray

### 4.1 Sin filesystem compartido

Debe mantenerse la ruta actual de transferencia por Ray:

```text
PC principal -> Ray object store -> temporal/cache del worker
worker -> artefacto de salida -> PC principal
```

Reglas:

- Los archivos deben viajar mediante `FileInputSpec`, `FileArtifact` y
  `ProjectOutputDirSpec`; una ruta Python sin contrato no implica transferencia.
- Debe definirse un tamaño máximo recomendado por objeto o shard.
- Los archivos grandes no deben duplicarse innecesariamente en memoria.
- Los resultados sólo se consideran entregados cuando se han persistido en el destino local.

Limpieza mínima:

```text
max_cache_bytes
cache_ttl
prune_on_cluster_down
```

Los temporales no cacheados se eliminan al finalizar cada tarea. Los elementos cacheados se
eliminan por TTL, presión de espacio o apagado administrado del cluster.

### 4.2 Con SSHFS

`shared_fs=True` debe pasar de ser una afirmación a ser una condición comprobada.

Configuración mínima:

```text
local_path
remote_mount_path
mount_options
mount_on_prepare
unmount_on_cluster_down
```

Para la primera versión se recomienda exigir la misma ruta absoluta en el controlador y los
workers. Un mapeo arbitrario de rutas puede añadirse cuando exista un caso real que lo necesite.

El flujo de preparación debe:

1. Comprobar que `sshfs` y FUSE están disponibles.
2. Instalar SSHFS sólo cuando exista un gestor soportado y autorización no interactiva.
3. Crear el punto de montaje.
4. Montar la carpeta.
5. Ejecutar una prueba bidireccional de lectura y escritura.
6. Confirmar que las rutas entregadas a Ray son visibles desde el worker.

Si la instalación requiere contraseña de `sudo`, la preparación debe detenerse con una
instrucción concreta; no debe abrir una petición interactiva oculta.

Con SSHFS los artefactos persistentes quedan físicamente en la PC principal, pero Ray puede
seguir creando metadatos y temporales de ejecución en los workers.

## 5. HPC mediante SSH y scheduler

La frontera real es:

```text
PC principal --SSH/rsync--> login node HPC
                               |
                               +-- scheduler allocation
                                     |-- nodo Ray head
                                     |-- nodo Ray worker
                                     |-- nodo Ray worker
                                     +-- filesystem HPC compartido
```

La PC principal y el HPC no comparten filesystem. Los nodos de una misma asignación HPC sí
comparten las rutas del cluster.

### 5.1 Transporte remoto

Debe incorporarse una capa SSH/rsync reutilizando la configuración común. Sus operaciones
mínimas son:

- ejecutar un comando remoto;
- crear un directorio remoto;
- enviar un manifiesto o archivo;
- recuperar un archivo o conjunto de resultados;
- comprobar hash y tamaño;
- consultar existencia;
- eliminar únicamente rutas resueltas dentro del directorio de campaña.

No hace falta introducir una librería SSH si el cliente `ssh` y `rsync` del sistema cubren el
contrato.

El staging debe estar basado en un manifiesto de contenido. Sólo se copian archivos ausentes o
cuyo hash sea diferente.

### 5.2 Directorio remoto de campaña

Cada campaña debe tener una raíz estable:

```text
<working_directory>/campaigns/<campaign_id>/
    campaign.json
    input/
    runtime/
    status/
    results/
    logs/
```

El identificador del scheduler y esta ruta deben persistirse en la base local de ejecutores. No
pueden existir únicamente en la memoria del proceso.

### 5.3 Script del scheduler

La primera implementación concreta debe ser Slurm. El script debe:

1. Activar el entorno validado.
2. Descubrir los nodos asignados.
3. Ejecutar la campaña por job array o iniciar un Ray head y sus workers.
4. Ejecutar un runner remoto basado en las APIs de `ms_flow`.
5. Escribir estado y resultados atómicamente.
6. Detener Ray al terminar o al recibir una señal del scheduler.

En un solo nodo puede utilizarse el modo Ray local actual. Para varios nodos, el cluster Ray es
administrado por el script dentro de la asignación; no es el `mode="local"` del controlador de
escritorio.

La PC principal no debe intentar conectarse directamente al puerto GCS de Ray en los nodos de
cómputo. El runner vive en el HPC y la PC se comunica exclusivamente por scheduler, SSH y
archivos de estado.

### 5.4 Job arrays primero; Ray pilot después

Para shards de docking independientes, Slurm job arrays ofrecen el primer backend más simple y
robusto. Ray multinodo debe añadirse como modo opcional cuando una campaña adaptativa, una carga
irregular o mediciones de rendimiento demuestren su utilidad.

Ambos modos deben compartir:

- transporte;
- manifiesto;
- directorio de campaña;
- polling;
- recuperación;
- estado persistente;
- limpieza.

### 5.5 Polling y recuperación

El polling consulta dos fuentes:

1. El scheduler, para conocer el estado de la asignación.
2. Un estado remoto pequeño, para conocer el progreso de la campaña.

Estado mínimo:

```text
campaign_id
remote_root
scheduler
scheduler_job_id
submitted_at
state
completed_shards
failed_shards
result_manifests
environment_fingerprint
last_update
```

`state.json` puede contener el resumen y debe escribirse mediante reemplazo atómico. Los
resultados por shard deben mantenerse separados o consolidarse en formatos adecuados como
JSONL o Parquet. No debe descargarse repetidamente un único JSON creciente.

La aplicación puede recuperar incrementalmente:

- estado y logs pequeños;
- manifiestos nuevos;
- paquetes de resultados terminados;
- resultados finales de la campaña.

Después de reiniciar AMDockVS, `ms_flow` debe reconstruir la vigilancia a partir del
`scheduler_job_id` y `remote_root` persistidos.

### 5.6 Limpieza remota

Nunca se elimina un resultado remoto antes de:

1. copiarlo al destino local;
2. comprobar tamaño o hash;
3. confirmar que el sink local lo persistió.

Las políticas mínimas son:

- conservar campañas activas;
- conservar campañas fallidas durante un TTL configurable;
- eliminar temporales de campañas completadas después de su recuperación verificada;
- permitir limpieza manual por `campaign_id`.

## 6. Problemas concretos que deben corregirse en `ms_flow`

1. `conda_bootstrap_command()` crea el entorno automáticamente; debe obedecer la política
   estricta.
2. `SSHHost` no forma parte efectiva del flujo de aprovisionamiento.
3. `RayWorkerConfig` no representa puerto, clave, directorio, montaje ni política del entorno.
4. `shared_fs` no instala, monta ni verifica SSHFS.
5. El caché Ray persistente no tiene límites ni pruning administrado.
6. El staging HPC usa copia local en lugar de transporte remoto.
7. El backend HPC no recupera artefactos como lo hace Ray.
8. Los handles del scheduler no son suficientemente persistentes para adoptar una campaña tras
   reiniciar.
9. `HPCWorkerConfig` no declara `command_context`, aunque el runtime intenta leerlo; Pydantic lo
   descarta.
10. HPC se registra separado de `compute`, mientras los jobs AMDock usan normalmente
    `executor="compute"`; debe existir una selección coherente de backend.
11. El runtime HPC actual ejecuta un callable por chunk y no levanta Ray multinodo.

## 7. Fases de implementación de `ms_flow`

### Fase MF-1 — LAN reproducible

- Integrar el modelo SSH común.
- Añadir la política `require`/`recreate`.
- Incorporar `working_directory`, puerto y clave.
- Validar imports y comandos declarados por la aplicación.
- Añadir pruning del caché Ray.

### Fase MF-2 — SSHFS opcional

- Configurar montaje y desmontaje.
- Detectar dependencias y permisos.
- Hacer la prueba bidireccional.
- Rechazar rutas no compartidas antes de iniciar Ray.

### Fase MF-3 — HPC remoto mínimo

- Implementar transporte SSH/rsync.
- Persistir `scheduler_job_id` y raíz remota.
- Añadir un perfil Slurm.
- Enviar manifiestos y recuperar resultados incrementalmente.
- Recuperar la vigilancia después de reiniciar.
- Implementar limpieza segura.

### Fase MF-4 — Ray pilot en HPC

- Generar script Ray multinodo desde la asignación.
- Lanzar el runner remoto.
- Integrar cancelación y apagado del cluster.
- Comparar rendimiento contra job arrays antes de declararlo predeterminado.

## 8. Criterios de aceptación de `ms_flow`

- Un host sin el entorno solicitado falla sin modificarlo.
- `recreate` reconstruye el entorno sólo cuando se solicita explícitamente.
- Ninguna contraseña queda guardada en configuración o logs.
- Una prueba remota ejecuta Python y un callable mínimo mediante Ray.
- El modo sin shared folder entrega entradas y devuelve resultados.
- El modo SSHFS detecta un montaje inexistente o roto antes de ejecutar tareas.
- `cluster_down` detiene workers y aplica la política de pruning.
- Una campaña Slurm continúa y puede ser adoptada después de reiniciar la aplicación.
- Los resultados HPC sólo se eliminan después de una recuperación verificada.
- Las pruebas no dependen únicamente de un scheduler simulado local: existe al menos una prueba
  de integración remota documentada.

---

# Parte II — AMDockVS

## 9. Responsabilidad de AMDockVS

AMDockVS debe definir:

1. Qué paquetes y programas necesita un worker químico.
2. Cómo se construyen los pipelines de preparación, docking y análisis.
3. Cómo se representan, dividen y consolidan los shards HTP.
4. Qué entradas y salidas se declaran mediante los contratos de datos de `ms_flow`.
5. Qué motores de docking y perfiles químicos están disponibles.
6. Cómo se calculan progreso, rankings, diversidad y selección de hits.
7. Cuándo los hits HTP se materializan en el modo normal.
8. Qué información se presenta en la UI.

AMDockVS no debe implementar su propio SSH, rsync, SSHFS, scheduler, base de jobs o gestión de
clusters Ray.

## 10. Modos `vs` y `htpvs`

### 10.1 Modo `vs`

Debe conservar la experiencia actual para una o varios miles de moléculas:

- moléculas materializadas;
- selección individual;
- tablas centrales;
- visualización 2D y 3D;
- preparación y docking interactivos;
- inspección de poses e interacciones.

Este modo representa el uso principal de AMDockVS y no debe degradarse para acomodar HTP.

### 10.2 Modo `htpvs`

Debe operar con:

- archivos fuente;
- shards o minibibliotecas;
- índices `(shard_id, record_index)`;
- estadísticas agregadas;
- histogramas y distribuciones;
- progreso por etapa;
- resultados compactos;
- materialización únicamente en el punto de promoción.

La ejecución remota recibe shards, no miles o millones de objetos de molécula independientes.
Esto disminuye inodos y aprovecha mejor las operaciones por lote de RDKit y otras herramientas.

## 11. Perfil de instalación del worker

No se recomienda crear todavía una distribución independiente `amdockvs-core`.

El worker sí necesita `ms_flow` porque:

- el runtime Ray ejecuta código de `ms_flow`;
- las tareas AMDock importan sus contratos de tasking, sinks y datos;
- el pipeline y los jobs deben seguir usando las APIs de Molsuite.

El worker no necesita `ms_components`. Actualmente existe una dependencia accidental porque
`amdockvs.configuration` importa `MonitorConfig` y `THEMES` desde `ms_components`, y algunos jobs
de dominio importan ese módulo de configuración. Esa unión debe eliminarse.

`ms_contactmap` sólo debe instalarse cuando se ejecuten etapas de interacciones o diagramas. Su
nucleo químico debe poder instalarse sin PySide6.

### 11.1 Extras propuestos

Una sola distribución puede ofrecer:

```text
amdock-vs                  dominio químico esencial
amdock-vs[ray-worker]      ms_flow[ray] y ejecución distribuida
amdock-vs[interactions]    núcleo headless de ms_contactmap
amdock-vs[ui]              PySide6, ms_components y visualización
amdock-vs[all]             instalación completa de escritorio
```

La separación requiere:

1. Mover tipos de configuración neutrales fuera de `ms_components`.
2. Evitar imports UI al cargar jobs o servicios de dominio.
3. Separar las dependencias core/UI de `ms_contactmap`.
4. Probar que el perfil `ray-worker` importa y ejecuta tareas sin servidor gráfico.

Una distribución `amdockvs-core` sólo se justifica si en el futuro tiene versionado, API o
consumidores independientes. Los extras resuelven el problema actual con menos complejidad.

## 12. Contrato que AMDockVS entrega a `ms_flow`

Por campaña, AMDockVS debe producir un manifiesto declarativo con:

```text
campaign_id
application_version
worker_install_profile
environment_lock_or_spec
required_imports
required_executables
pipeline_steps
input_artifacts
output_contract
resource_requirements
retry_policy
```

Los parámetros químicos deben ser serializables. Las rutas deben declararse mediante los
contratos `FileInputSpec`, `FileArtifact` o `ProjectOutputDirSpec`, no como strings cuya
transferencia quede implícita.

El perfil de instalación debe identificar el wheel exacto o un repositorio de wheels. Instalar
simplemente “la última versión” no permite reproducir una campaña.

## 13. Ejecución HTP sobre LAN

AMDockVS debe:

1. Importar o fragmentar la fuente en shards.
2. Construir el pipeline de pasos químicos.
3. Declarar cada shard como artefacto de entrada.
4. Enviar jobs por shard o por grupos de shards.
5. Consolidar estado y resultados sin materializar moléculas individuales.
6. Aplicar ranking, filtros, diversidad o clustering sobre representaciones compactas.
7. Promover únicamente los hits finales al almacenamiento normal.

Con SSHFS, los jobs pueden operar sobre rutas compartidas. Sin SSHFS, los shards viajan mediante
el transporte Ray ya proporcionado por `ms_flow`.

El tamaño del shard debe ser configurable y medirse con los motores reales. No debe fijarse un
número universal antes de tener benchmarks de I/O, memoria y tiempo por molécula.

## 14. Ejecución HTP sobre HPC

AMDockVS debe construir una campaña autocontenida para el runner remoto:

- receptores y grids;
- shards de ligandos;
- pipeline normalizado;
- configuración de motores;
- semillas y parámetros reproducibles;
- contrato de resultados;
- metadatos para consolidación y promoción.

Dentro del HPC el runner utiliza las APIs de `ms_flow` y el filesystem compartido del cluster.
La aplicación de escritorio no controla directamente cada tarea Ray; consulta la campaña y
recupera sus productos mediante el backend HPC de `ms_flow`.

Para docking independiente, AMDockVS debe soportar primero job arrays. El modo Ray pilot puede
usar el mismo manifiesto y pipeline cuando se implemente.

## 15. Resultados y promoción

Mientras la campaña sea HTP, AMDockVS conserva por shard:

- identificador estable;
- estado;
- número de registros válidos y fallidos;
- scores y columnas mínimas de ranking;
- descriptores o embeddings necesarios;
- referencias a poses o artefactos;
- procedencia y parámetros del protocolo.

Los resultados pueden consolidarse en tablas compactas. Sólo al seleccionar los hits finales se
crean las entidades normales de molécula, docking result y pose necesarias para la UI actual.

El tamaño de promoción debe ser configurable —por ejemplo 500, 1000 o más— y tener una
confirmación explícita antes de crear un número grande de registros y archivos individuales.

## 16. UI

El modo `vs` conserva el layout actual y puede mejorarse sin convertirlo en una UI de campañas
masivas.

El modo `htpvs` debe mostrar principalmente:

- fuentes y shards;
- progreso global y por etapa;
- uso de workers o scheduler;
- distribuciones de propiedades y scores;
- clustering, diversidad y espacio químico;
- errores agregados;
- reglas de selección;
- punto de promoción al modo materializado.

No debe intentar mostrar una fila o una estructura renderizada por cada ligando de la biblioteca.
La visualización molecular detallada aparece después de seleccionar o promover un subconjunto.

## 17. Cambios concretos necesarios en AMDockVS

1. Separar configuración de dominio y configuración UI.
2. Eliminar `ms_components` del camino de imports de los jobs.
3. Separar o hacer opcional la UI de `ms_contactmap`.
4. Definir extras de instalación para escritorio y worker.
5. Generar un perfil verificable de herramientas químicas por campaña.
6. Asegurar que todas las entradas/salidas remotas usan contratos de datos de `ms_flow`.
7. Terminar el modelo de shards y el pipeline HTP antes de optimizar el scheduler.
8. Implementar consolidación incremental de resultados por shard.
9. Implementar materialización/promoción final con límite configurable.
10. Permitir seleccionar `compute` local, Ray LAN o campaña HPC sin que los jobs químicos
    conozcan los detalles del transporte.

## 18. Fases de implementación de AMDockVS

### Fase AMD-1 — Worker headless

- Desacoplar configuración y UI.
- Definir extras `ray-worker`, `interactions` y `ui`.
- Crear una prueba de instalación e import headless.
- Definir el manifiesto de requisitos del worker.

### Fase AMD-2 — HTP local/Ray

- Completar shards y pipeline por lotes.
- Ejecutar shards mediante `executor="compute"`.
- Consolidar resultados compactos.
- Probar LAN con y sin carpeta compartida.
- Promover un conjunto pequeño de hits al modo normal.

### Fase AMD-3 — Bundle HPC

- Generar el manifiesto autocontenido de campaña.
- Declarar inputs y outputs transferibles.
- Ejecutar mediante el perfil Slurm de `ms_flow`.
- Recuperar progreso y resultados por shard.
- Reanudar la monitorización después de reiniciar AMDockVS.

### Fase AMD-4 — Estrategias adaptativas

- Aplicar clustering, similitud, propiedades o descriptores para seleccionar nuevas rondas.
- Comparar job arrays y Ray pilot.
- Añadir automodelado con hits iniciales únicamente cuando el flujo anterior sea estable.

## 19. Criterios de aceptación de AMDockVS

- El modo normal sigue funcionando con una o varios miles de moléculas y conserva la inspección
  individual.
- El perfil de worker se instala sin PySide6 ni `ms_components`.
- Una tarea de docking headless funciona en un worker Ray.
- HTP no crea entidades individuales para toda la biblioteca.
- Un shard puede reintentarse sin duplicar resultados.
- La misma campaña puede usar Ray con transferencia o con carpeta compartida.
- El bundle HPC contiene todo lo necesario para ejecutar sin la UI.
- Los resultados recuperados conservan procedencia, protocolo y correspondencia con el registro
  original del shard.
- La promoción crea únicamente el subconjunto seleccionado y lo hace visible en la UI normal.

---

# Parte III — Orden conjunto y decisiones cerradas

## 20. Orden recomendado

1. `ms_flow`: modelo SSH y política estricta del entorno.
2. AMDockVS: perfil headless sin dependencias UI.
3. `ms_flow`: completar Ray LAN sin shared folder y pruning.
4. AMDockVS: campaña HTP por shards sobre Ray.
5. `ms_flow`: SSHFS opcional con validación real.
6. `ms_flow`: transporte SSH/rsync, persistencia y perfil Slurm.
7. AMDockVS: bundle HPC, polling y recuperación integrados.
8. `ms_flow`: Ray pilot multinodo opcional.
9. AMDockVS: selección adaptativa y automodelado posteriores.

## 21. Decisiones cerradas para la primera implementación

- No almacenar contraseñas SSH.
- No crear automáticamente entornos salvo que el usuario seleccione `recreate`.
- No crear todavía una distribución `amdockvs-core`.
- Mantener `ms_flow` en los workers.
- Excluir `ms_components` de los workers.
- Hacer `ms_contactmap` opcional y headless cuando corresponda.
- No usar SSHFS en HPC.
- No conectar la UI directamente al cluster Ray interno del HPC.
- Implementar Slurm job arrays antes del Ray pilot multinodo.
- No eliminar resultados remotos sin recuperación verificada.
- Mantener el modo `vs` y su experiencia actual separados del almacenamiento HTP.

## 22. Resultado esperado

Al completar estas fases, AMDockVS tendrá tres rutas coherentes sin duplicar la lógica química:

```text
Modo VS
  -> ejecución local o Ray
  -> moléculas materializadas
  -> análisis visual individual

Modo HTP LAN
  -> shards
  -> Ray con transferencia o SSHFS
  -> selección compacta
  -> promoción de hits a VS

Modo HTP HPC
  -> bundle remoto
  -> scheduler + job arrays o Ray pilot
  -> polling y recuperación incremental
  -> promoción de hits a VS
```

La diferencia entre estas rutas está en almacenamiento, transporte y ejecución. Los pasos de
dominio químico deben seguir siendo los mismos y estar expuestos mediante las APIs de Molsuite.
