# Estructura recomendada de `amdock`

Esta app se está migrando a una forma más simple e intuitiva. La prioridad no es “la arquitectura más pura”, sino que un programador nuevo pueda abrir `../src/amdockvs` y entender rápido dónde tocar.

## Regla principal

La ruta feliz debe estar en pocos sitios:

- `runtime.py`: fachada pública de negocio
- `jobs/`: jobs y tasks por funcionalidad
- `queries.py`: lecturas de dominio del proyecto
- `io/`: parsing y materialización de archivos
- `summaries.py`: DTOs públicos

La UI no debe esconder lógica de negocio. Debe llamar al runtime.

## Excepción deliberada: docking

Docking sí tiene variación real y frecuente. Por eso su extensión se concentra en
`src/amdockvs/docking/` mediante tres contratos pequeños, sin crear un framework de
plugins adicional:

- `programs.py`: capacidades, modelo de configuración y recursos del programa.
- `preparations.py`: perfil de preparación de ligando, receptor y, opcionalmente, shard.
- `registry.py`: registro y despacho del runner que ejecuta un chunk.
- `vina.py`, `qvina.py`, `gnina.py`, `adgpu.py`: implementación de cada runner incorporado; `autogrid.py` genera los mapas AD4 que comparten Vina (`ad4`) y AutoDock-GPU.

Los jobs de MolSuite (`preparation_jobs.py`, `jobs.py` y `shard_jobs.py`) son la
infraestructura estable. No deben importar condicionalmente cada engine ni contener
ramas `if engine == ...`; reciben el nombre registrado y despachan por los contratos
anteriores. Los nombres históricos de task de Vina se conservan únicamente para no
romper jobs persistidos.

### Añadir un docking engine

Un engine nuevo define un modelo Pydantic propio, un `DockingProgramSpec` y un runner:

```python
from pydantic import BaseModel, ConfigDict, Field

from amdockvs.docking import DockingProgramSpec, register_docking_engine


class MyEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    iterations: int = Field(default=20, ge=1)


MY_ENGINE = DockingProgramSpec(
    key="my_engine",
    label="My engine",
    workflow_key="vina",
    preparation_engine="ad4",
    docking_engine="my_engine",
    config_model=MyEngineConfig,
    protocol_identity_keys=("iterations",),
    resource_resolver=lambda config: {"cpu_required": 2},
)


def run_my_engine(payload: dict) -> list[dict]:
    # payload["pairs"] contiene los pares resueltos por el job de MolSuite.
    # payload["iterations"] ya fue validado y combinado desde engine_config.
    ...


register_docking_engine(MY_ENGINE, run_my_engine)
```

Las capacidades `requires_ligand_preparation`,
`requires_receptor_preparation` y `requires_binding_site` controlan readiness y
construcción de lotes. `supports_shards` debe declararse solo si el runner acepta el
contrato de campañas por shards.

Si la preparación no es `ad4`, se registra junto al programa:

```python
from amdockvs.docking import PreparationProfile, register_preparation_profile

register_preparation_profile(
    PreparationProfile(
        key="my_prep",
        prepare_entities=prepare_my_entities,
        prepare_shard=prepare_my_shard,  # opcional
    )
)
```

Vina, QuickVina, GNINA y AutoDock-GPU comparten correctamente el perfil `ad4`: los cuatro consumen
PDBQT compatible. Compartir la preparación evita duplicación; no implica compartir
el runner ni la configuración del docking.

## Regla de contexto

AMDock no resuelve layout del proyecto por su cuenta.

Las rutas de trabajo salen del contrato de MolSuite:

- `ligands`
- `receptors`
- `docking_results`
- `exports`
- `jobs`

Los jobs deben consumir `project_resources` desde `config`, no recomponer `data/...` o `results/...` localmente.

## Qué va junto

Cada feature del backend debe vivir junta:

- `jobs/ingestion.py`
- `jobs/descriptors.py`
- `jobs/docking.py`

En cada módulo de `jobs/` se permite tener:

- modelos `Params`
- helpers pequeños propios del job
- `@task(...)`
- `@job(...)`

Esto evita perseguir el flujo entre `job.py`, `task.py`, `service.py`, `builder.py` y `registry.py` cuando todavía no hace falta.

## Qué no hacer por ahora

No crear capas preventivas tipo:

- `repository/`
- `services/`
- `builders/`
- `orchestrators/`
- `domain/infra/application/` artificial

Solo merece separarse si un módulo deja de ser legible por sí mismo o si una pieza se reutiliza claramente entre varias features.

## Criterio de separación

Separar un módulo cuando ocurra una de estas:

- supera un tamaño que ya dificulta leer el flujo completo
- mezcla dos responsabilidades reales distintas
- una función se reutiliza desde más de una feature
- una dependencia pesada merece quedar aislada, como I/O o parsing

Por eso `io/loaders.py` sí tiene sentido aparte: no es “arquitectura extra”, es una frontera real entre parsing/materialización y jobs.
El layout del proyecto no vive ahí; vive en MolSuite.

## Cómo debería verse un caso de uso nuevo

Si mañana añadimos un job de scoring posterior a docking, la ruta esperada sería:

- `jobs/scoring.py`
- método público en `runtime.py`
- lecturas de resultados en `queries.py` si hacen falta
- DTO nuevo en `summaries.py` solo si la API pública lo necesita

No debería requerir crear un subárbol entero nuevo.

## Contrato mental para el equipo

- Si quiero ejecutar algo de dominio: voy a `runtime.py`
- Si quiero cambiar cómo se define un job: voy a `jobs/`
- Si quiero cambiar cómo se leen datos del proyecto: voy a `queries.py`
- Si quiero tocar parsing o archivos: voy a `io/`
- Si quiero saber qué devuelve la API: voy a `summaries.py`
- Si quiero construir una vista de dominio: voy a `ui/`, pero consumo `runtime.py`, no `molsuite.core.*` ni SQL directo

Ese mapa es más valioso para esta app que una separación demasiado sofisticada.
