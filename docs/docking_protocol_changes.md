# Una configuración vigente por programa de docking

Docking conserva una única configuración por programa en el proyecto. Redocking
permite varios protocolos para comparar resultados de pruebas.

Si se cambia la configuración de un programa que ya tiene resultados de docking,
`docking.run(...)` y `docking.run_shards(...)` lanzan
`DockingProtocolConflictError` antes de enviar trabajos o borrar resultados. La UI
ofrece solamente **Reemplazar resultados de docking** y **Cancelar**.

Al confirmar mediante `replace_existing=True`, se eliminan todos los resultados
de docking de ese programa en el proyecto, incluidas poses, análisis derivados,
contadores y recibos/archivos de resultados por shards. La ejecución comienza de
nuevo sobre el alcance solicitado. Se conservan los resultados de redocking, los
de otros programas y la biblioteca preparada.

El borrado sucede antes de enviar la ejecución nueva, después de validar la
solicitud. Si la ejecución nueva falla o se cancela, los resultados borrados no se
recuperan. La UI lo indica expresamente antes de confirmar.

```python
from amdockvs.docking import DockingProtocolConflictError

try:
    runtime.docking.run(program="qvina", exhaustiveness=1, num_modes=3)
except DockingProtocolConflictError as exc:
    print(exc.code)                 # docking_protocol_conflict
    print(exc.previous_protocols)   # configuraciones anteriores
    print(exc.requested_protocol)   # configuración solicitada

# Solo después de decidir reemplazar los resultados anteriores:
runtime.docking.run(
    program="qvina", exhaustiveness=1, num_modes=3,
    replace_existing=True,
)
```

`check_protocol(...)` comprueba el cambio sin enviar trabajos ni borrar datos.
El servicio comprueba todos los programas antes de lanzar la primera etapa. En
off-target, el reemplazo ocurre en la etapa de referencia; la etapa dependiente
conserva sus resultados nuevos. Para cambiar la configuración desde una llamada
directa a la etapa dependiente, primero se debe repetir la referencia.

Una configuración sin cambios puede reanudarse con `skip_existing=True`, incluso
si el proyecto guardó una versión anterior de su hash. El cambio de configuración
se detecta con los parámetros de búsqueda y salida (incluidos número de poses,
semilla y rango de energía); el número de CPU no crea otra configuración.
La API usa la configuración validada para los metadatos, aunque el llamador omita
los metadatos o suministre un hash anterior.
