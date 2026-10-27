`scripts/` contiene utilidades auxiliares, demos no oficiales y scratch.

Reglas:

- no usar `scripts/` como interfaz publica del producto
- preferir `amdockvs` o `python -m amdockvs` como entrypoint oficial
- mover a `examples/` cualquier demo que sea reproducible y apta para usuarios
- todo archivo aqui debe asumirse auxiliar, experimental o legacy aunque siga siendo util para desarrollo local
