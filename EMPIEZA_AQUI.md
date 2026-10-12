# Primera sesión de Job Radar

DESACTUALIZADO: esto describía la primera versión. Ahora `agent.py` contiene su propio bucle de llamadas a la API de Anthropic y se ejecuta solo desde la terminal, sin Copilot ni Claude Code en tiempo de ejecución. Busca ofertas de forma programática en las APIs oficiales de cinco empleadores. Lee el README.md para la versión actual; esta guía sirve solo como introducción a las herramientas deterministas de `radar.py`.

1. Abre esta carpeta en VS Code con File > Open Folder.
2. Abre Terminal > New Terminal.
3. Ejecuta `python run_demo.py`. Si Windows reconoce `py` en lugar de `python`, usa `py run_demo.py`.
4. Verás una recomendación, una oferta pendiente de verificación y tres descartadas. Todas son ficticias. El demo usa evaluaciones preescritas; no ejecuta IA.
5. Lee `AGENT_INSTRUCTIONS.md` y `docs/LEARNING_GUIDE.md`. Tu primera pregunta: ¿por qué no recomendar una oferta que no menciona sponsorship?
6. Abre Copilot en modo Agent y escribe: Lee AGENT_INSTRUCTIONS.md. Explícame qué decisiones toma el modelo y qué controles realiza Python. Después evaluaremos una oferta real que voy a pegar. No modifiques el código ni registres aplicaciones.
7. Pega la descripción completa y el enlace de una oferta real. Revisa las citas y cuestiona la conclusión.

Tu perfil está en private/profile.json. No inventamos tu fecha de graduación, disponibilidad ni autorización actual. El historial suministrado está en private/history_reference.md; las cinco aplicaciones históricas no se importaron automáticamente al contador nuevo.

El ZIP contiene datos personales en private/. Esa carpeta está excluida por .gitignore: no subas el ZIP completo a un repositorio público.

La guía en inglés incluye comandos, límites, pruebas y pasos para GitHub. Los registros de aprendizaje los completarás con lo que realmente revises y cambies.
