`data/` es un workspace local, no una interfaz oficial del producto.

Su funcion es alojar datasets grandes, proyectos temporales y salidas de prueba que ayudan al desarrollo local pero no deben definir la narrativa principal del repo.

Politica:

- no versionar datasets pesados ni proyectos de trabajo en git
- mantener en el repo solo `data/README.md` y `data/fixtures/`
- usar `data/fixtures/` para muestras pequenas y controladas que deban quedar versionadas
- tratar carpetas como `project_*`, `pdemo*` o datasets grandes como material local descartable
- si un ejemplo oficial necesita datos, debe usar fixtures pequenas o generarlos temporalmente
