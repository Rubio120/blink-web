BLINK MOTEL - Sistema de recepción mejorado

1. Requisitos: Python 3 + Flask.
   pip install flask

2. Copie app.py, la carpeta templates y la carpeta static en la misma carpeta de su proyecto.
   Puede conservar su reservas.db actual: la aplicación migra las columnas automáticamente.

3. Inicie:
   python app.py

4. Abra:
   Sitio público: http://127.0.0.1:5000
   Recepción: http://127.0.0.1:5000/admin

5. Acceso local predeterminado:
   usuario: admin
   contraseña: blink123

IMPORTANTE: antes de publicar en Internet configure variables de entorno:
   set SECRET_KEY=una-clave-larga-y-unica
   set ADMIN_USER=admin
   set ADMIN_PASSWORD=una-contrasena-segura
   set WHATSAPP_NUMBER=595986333704

FUNCIONES:
- Tarifas base de 90 min y adicionales de 30 min.
- Hab. 1-7: Gs. 45.000; adicional 30 min Gs. 20.000.
- Hab. 8-13: Gs. 50.000; adicional 30 min Gs. 20.000.
- Hab. 14-15: Gs. 70.000; adicional 30 min Gs. 30.000.
- Panel en tiempo real con temporizadores.
- Alertas cuando faltan 15 minutos.
- Extensión +30 minutos desde recepción.
- Check-in / check-out.
- Estado Limpieza / Mantenimiento / Libre.
- Reservas manuales desde recepción.
- Búsqueda y filtros.
- Ingresos del día.
- Agenda diaria.
- WhatsApp.
- Registro de acciones.
- Backup diario de la base de datos y descarga desde el panel.
- Login con sesión.

VERSIÓN ORIGINAL + MEJORAS
--------------------------
Esta edición conserva el diseño público oscuro/dorado original de BLINK MOTEL y mantiene las mejoras administrativas.
Incluye verificación de disponibilidad por horario en la página pública, tarifas por adicionales de 30 minutos y bloqueo backend contra reservas cruzadas.


=== BLINK MOTEL - MEJORAS OPERATIVAS ===
- Seguridad: si no configurás ADMIN_PASSWORD/ADMIN_PASSWORD_HASH, el sistema genera una contraseña fuerte la primera vez.
  La encontrarás en CLAVE_ADMIN_INICIAL.txt dentro de la carpeta del proyecto. Guardala en un lugar seguro y luego eliminá ese archivo.
- Las reservas web pendientes vencen automáticamente a los 15 minutos si recepción no las confirma.
  Para cambiarlo: variable PENDING_EXPIRE_MINUTES.
- El panel consulta cambios cada 5 segundos y se actualiza automáticamente.
- Al entrar una reserva nueva, el panel muestra aviso y emite un sonido.
- Una habitación ocupada avisa al quedar 15 minutos o menos.
- Los adicionales desde recepción son únicamente +30 min: habitaciones 1-13 Gs. 20.000; habitaciones 14-15 Gs. 30.000.
- Se crea un backup diario de reservas.db y se conservan los últimos 30 días.
- Para producción con HTTPS: configurar HTTPS_ONLY=1, SECRET_KEY y credenciales mediante variables de entorno.

PRUEBA FINAL RECOMENDADA ANTES DE PUBLICAR:
1) Crear reserva web y abrir WhatsApp.
2) Confirmar en panel.
3) Hacer check-in.
4) Probar +30 min y verificar precio.
5) Hacer check-out: debe pasar a Limpieza.
6) Marcar Libre.
7) Crear una reserva pendiente de prueba y verificar vencimiento.
8) Probar desde PC y celular en la misma red.
