# Plan de mejoras (agosto 2026)
- opcion para mostrar la proteina en susperficie electrostatico (default)
- al menos los residuos dentro de la caja esten en otro color







# Plan de revisiones (no usar como referencia)

Este plan de revisiones tiene como objetivo que yo vaya anotando los detalles que aparecen/faltan de amdock para ir
redondeandolo para su lanzamiento

## Import

- [x] comprobar que import ligand no congele la ui
- [x] comprobar que se estén guardando toda la info
- [ ] comprobar que si hay actividad se defina y capture correctamente la info.
- [x] comprobar que el importador de ligando funcione bien y tenga lo que necesite
- [x] configurar las opciones avanzadas con tabs en el dialog de importacion... para aplicar clustering, qsar, etc.
  Deberiamos usar una taba para la parte de importacion en vez de un dialog? tendriamos mas espacio, pero tambien 
  algo menos natural, aunque la importacion con tantos elementos definitivamente no es natural tampoco. 
  `RESUELTO: el dialog es mas natural, ademas que propicia un mejor flujo de trabajo porque es mas efimero y 
  apartado del resto de la UI que tiene un enfoque mas de proceso`

- [x] comprobar que import receptor no congele la ui, en especial porque este manipula los receptores en la misma ui.
  aplica lo mismo que para ligand en cuanto a la UI
- [x] comprobar que se guarde la info de los receptores
- [x] selector de cadenas y demás en el receptor (ahora se listan pero no se puede escoger)

Molecule tools
Build

- [ ] generar una especie de job compuesto de química de ligandos. Esto disminuye la presión sobre la db y el disco ya que
  aplica de una vez operaciones muy rápidas. Por ejemplo, protonar, generar 3d y conformeros trabajan juntos
  prácticamente, es especial los primeros dos.
- [ ] generar 3d para proteína debe ser al server de ESMFold2 y en thread, debería ser simple. ahora mismo, no tenemos la
  parte de la secuencia ni aceptamos este tipo de archivos, entonces hay que hacer un plan de implementacion.
- [ ] fix receptor debería usar pdbfixer, debería estar disponible además en la importación para hacerlo de una vez (
  opcional)
- [ ] protonar debería ser con pdb2pqr o reduce
- [ ] minimizar con openmm (simple, avanzado más adelante)

Filter

- [x] probar la parte de clustering. No se si aquí aplicará la parte de similitud estructural (se puede implementar mas
  adelante)
- [x] mejorar la parte de propiedades. Debería descartarse la preview si toma mas de cierto tiempo (no tengo idea de 
  como
  hacer esto, de pronto hasta se puede eliminar y solo mostrar un warning error cuando no se genere ningún resultado
  válido, aunque esto solo sería posible si se hace el filtrado :()
- [ ] duplicados, probablemente con inchikey para ligandos, secuencias para proteínas

Align

- [ ] para ligandos habría que calcular fragmentos o regiones usando marcoff o algo así
- [ ] alinear secuencias de proteínas. Visualización en pymol, simple, widgets y opciones mas avanzadas mas adelante

Qsar
Activity

- [ ] probar que las actividades se cargan como debe
- [ ] gráfico de representación async y ajustado. Mas adelante podemos configurar un gráfico dinámico donde se puedan
  definir diferentes ejes
- [ ] importación de csv para datos. Seleccionar la key qué coincide (por ejemplo, name, canonical smiles, inchikey)
- [ ] definición manual de ligandos. Creo que mediante diálogo emergente )
  Models
- [ ] crear dataset de pruebas simple, con relativamente pocos ligandos
- [ ] hacer el automodel como lo venimos haciendo con métricas simples para cada uno
- [ ] implementar chemprop si no es muy complicado, de lo contrario mas adelante
- [ ] definición del data set, division por naturaleza química, revisar
- [ ] entrenar modelos simples
  Predicciones
- [ ] visualizar predicciones, incluir gráfico que muestre donde se ubica respeto al modelo con error igual a la diferencia
  100-confidence
- [ ] si se aplican desde la importación, debe haber opción de persistir, si se marca se pone el valor en la tabla, de lo
  contrario no


Docking
Docking studio

- [x] bastante bien, solo hay que revisar si tenemos la mejor estructura de selección
- [x] Hay que mostrar mejor cuando un ligando / receptor falla en prepararse, ahora mismo pasa silencioso
- [x] debe haber un botón checkbox para la parte de interacciones
- [x] pudieran haber un threshold para VS qué no persista ninguna molécula con afinidad > -8.5 kcal/mol (vina), creo que a
  nivel de programa. Resuelto por decisión: no filtrar persistencia desde Docking Studio; conservar resultados para auditoría
  y aplicar filtros query-side en Results.
- [x] el paso 1 define el tipo de experimento y los tipos receptor/ligando. Por ahora: receptor=protein,
  ligando=small_molecule; off-target/cross-docking no se exponen como modos porque se cubren con combinatoria todos-vs-todos.
- [x] los programas disponibles en el paso 1 se filtran segun experimento, receptor y ligando seleccionados.
- [x] paso 1 separa intencion: Docking usa un Software Run Set con seleccion multiple; Redocking usa un Validation Protocol Set acotado.
- [x] checkbox de programa visible solo en Docking como seleccion de software; Redocking usa solo el set de validacion.
- [x] Docking encola un job independiente por software/configuracion seleccionada y persiste `metrics.protocol`; `skip_existing` y nombres de salida distinguen configuraciones del mismo engine.
- [x] separar backend: `protocol_metadata` solo describe resultados; engine/preparation/backend siguen siendo parametros de ejecucion.
- [ ] integrar backend real de rescoring/reranking para que el campo `rescoring` deje de ser placeholder.
Results
- [x] simplificar Docking Results: quitar gráficos embebidos, evitar overflow y dejar scroll en detalle/interacciones
- [ ] si se reactivan gráficos de resultados/interacciones, moverlos a un dock compartido con PyMOL/Distribution, no al panel de Results
- [x] en results debemos aplicar filtros también similar al anterior
- [x] de pronto aplicar visualización de afinidad vs propiedades o algo así
- [x] para las interacciones usar ms_contactmap (detector propio, sin dependencias con licencia restrictiva) y JSON por pose
- [x] mostrar graficos de interacciones numero de hits vs tipo de interaccion y numero de interacciones por residuo
- [x] implementar las metricas principales: LE, LLE/LiPE, Fit Quality (FQ), BEI, SEI, predicted Ki.
  Pendiente si aporta: SILE y LELP.

Redocking
- [x] hay que cambiarlo porque ahora se asume que se va a computar desde aqui y se supone que en DS tenemos ya la seleccion
  de referencias (tanto individual como incluida en todos los ligandos
  Nota: la ejecucion redocking se selecciona en Docking Studio paso 4; el panel separado queda como Redocking Results.
- [x] aqui la vista debe ser comparativa entre la proteina orginal y la que se uso para el docking (en caso que se haya
  hecho algua modificacion como minimizacion
- [x] ordenar resultados por receptor -> ligando -> protocolo -> pose; quitar columna Min hasta que haya una señal real de minimización
- [x] corregir RMSD de redocking: medir en el marco del receptor, sin realinear/superponer la pose contra la referencia
- [x] representar múltiples programas/protocolos con una columna Protocol dentro del mismo par receptor-ligando
- [ ] graficos de validacion por protocolo: success@1/2A, mediana/P90 RMSD, curva acumulativa, best-of-N y rank recovery.
- [x] si ambas proteinas no tienen diferencias, entonces se muestra una con los dos ligandos, de lo contrario se muestran 2
  superpuestas con sus respectivos ligandos.
-

Worflow

- [ ] esta congelando la ui, revisar
- [ ] la parte visual debe acomodarse al ancho visual del widget de forma dinamica. por ejemplo, debe tener como 2, 3 o 4
  elementos de ancho y luego hacer como una serpiente y acomodarse hacia abajo en vez de para el lado, el scroll se
  siente mas natural
- [ ] si un paso se completa, pero falla algun elementos (por ejemplo, en vez de preparar 100 ligandos se prepararon 99, el
  paso debe ser naranja y decirlo
