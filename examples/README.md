# Examples

Los ejemplos usan la API pública actual:

```python
from amdockvs import AMDockVSRuntime
```

No se mantienen ejemplos basados en `amdockvs.api.AMDockRuntime`.

- `pipeline_demo.py`: flujo corto de importación, preparación y docking
- `runtime_quickstart.py`: ejemplo más explícito de envío y espera de jobs

## Tutorial guiado en PySide6

`guided_tutorial_demo.py` es una app independiente: resalta controles, atenúa el resto
de la ventana, deja interactuar con el control señalado y avanza al completar la acción.
Incluye Atrás, Salir/Esc y reinicio; recalcula las posiciones al cambiar el tamaño.
Usa el componente compartido `ms_components.ms_tutorial`. Tiene dos recorridos:
informativo (solo Siguiente/Atrás, sin acciones) e interactivo (espera señales).
Los slots de la aplicación de prueba no contienen llamadas al tutorial.

Ejecutar desde la raíz del repositorio:

```bash
conda activate MSDD_312
PYTHONPATH="../ms_components/src:../ms_flow/src:../ms_env/src${PYTHONPATH:+:$PYTHONPATH}" \
  python examples/guided_tutorial_demo.py
```

Estas rutas corresponden a los checkouts actuales (`ms_components` y `ms_flow`),
equivalentes a los nombres anteriores `molsuite_components` y `molsuite_flow`.
Si esos paquetes ya están instalados en el entorno, no hace falta `PYTHONPATH`.

La demo utiliza **el `ProjectsMenuWidget` real** y **`ProjectCatalogBackend` de la API
de Molsuite**. Crea un catálogo y un proyecto temporales bajo su propio `MOLSUITE_HOME`,
que elimina al cerrar normalmente. No utiliza el catálogo personal. Abre el proyecto
de práctica con doble clic para pasar a una pantalla de trabajo ilustrativa.
«Cargar datos de muestra» y «Validar ejemplo» solo cambian el estado visual: no importan
moléculas, no envían jobs y no calculan docking. La creación de proyectos está desactivada
para mantener el recorrido dentro de una única ventana.

Comprobación de interacción con Qt y el catálogo real:

```bash
QT_QPA_PLATFORM=offscreen \
  PYTHONPATH="../ms_components/src:../ms_flow/src:../ms_env/src${PYTHONPATH:+:$PYTHONPATH}" \
  python -m pytest -q examples/test_guided_tutorial_demo.py
```

### Viabilidad en AMDockVS y ms_projects

Es viable con Qt Widgets, sin añadir una dependencia de tutoriales. `TutorialStep`
describe el destino, el texto y un evento opcional de avance. `TutorialTarget` enlaza
el destino con un widget o resolver y una función opcional de navegación de UI.
`TutorialController` observa señales existentes desde fuera de los slots. Los pasos
informativos bloquean la interacción con el destino; los interactivos dejan pasar
los clics reales. No depende de posiciones fijas ni de localizar controles por su texto.
El contenido admite Python o TOML; los enlaces con widgets/señales se definen en Python.
La guía de uso está en `../ms_components/docs/ms_tutorial.md`, y allí hay otra demo
independiente con ambos modos y un recorrido cargado desde TOML.

La integración propuesta, comprobada contra el código actual:

- **Componente compartido**: `ms_components.ms_tutorial` ya contiene el controlador
  agnóstico a la aplicación; cada aplicación registra sus destinos, eventos y pasos.
- **Proyectos**: `src/amdockvs/ui/shell/projects.py` ya crea `ProjectsMenuWidget` y
  conecta `project_requested`. Se pueden encadenar los recorridos con esta señal y
  `project_created`, ambas públicas. La demo ejercita el primer enlace.
- **Diálogo y ventana principal**: AMDockVS aloja el navegador en un `QDialog` que se
  oculta al abrir un proyecto. Necesita una capa por ventana y transferir el paso al
  cambiar de ventana. `ms_tutorial` ya admite ese cambio de host y lo prueba con un
  diálogo modal; esta demo de AMDock sigue incrustando el navegador para probar el
  enlace en pequeño. Falta registrar el recorrido en la aplicación científica real.
- **Pantallas dinámicas**: `ToolCoordinator.open_tool` y
  `MainContentWidget.open_or_focus_view` permiten mostrar la pantalla antes de resolver
  su destino. `TutorialTarget` permite registrar esa navegación y resolver el widget
  al entrar al paso, porque docks y diálogos pueden crearse tarde o destruirse.
- **Acciones de química**: usar las APIs existentes de AMDockVS/Molsuite y verificar
  su resultado. `monitor_bridge.job_finished` ya publica ID y estado: el tutorial puede
  conectarse mediante `TutorialEvent` y comprobar el job esperado y el éxito sin cambiar
  `JobFeedbackController.on_job_finished`; enviar un job no completa el paso.
- **Destinos estables**: `title_label` y `project_tree.view` existen actualmente como
  atributos, pero convendría ofrecer un método público de destinos del tutorial en
  `ms_projects` para no depender de la estructura interna del componente.
- **Progreso**: guardar versión e identificador del tutorial en preferencias de UI;
  ofrecer «Ayuda → Tutoriales» y un proyecto de práctica separado. No modificar el
  proyecto científico del usuario para enseñar el flujo.

Primera integración aconsejada: abrir/crear un proyecto → importar un receptor y pocos
ligandos de ejemplo → comprobar la importación. Extender a preparación y docking después
de integrar el recorrido entre ventanas. Cambiar de proyecto con uno abierto lanza otro
proceso: continuar allí necesitaría guardar y recuperar el progreso desde la aplicación.
Quedan por probar los docks flotantes, foco/accesibilidad con lectores de pantalla,
escalado HiDPI y el visor PyMOL. La demo conserva el estado de los datos al reiniciar.
