# Primera sesión de Job Radar

Esta versión usa Copilot en modo Agent o Claude Code como motor de IA. Python aporta herramientas para leer, validar y guardar las evaluaciones. No es todavía una aplicación independiente que busque ofertas automáticamente.

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
