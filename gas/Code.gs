/**
 * ID de la hoja de cálculo y configuración
 */
const SPREADSHEET_ID = "1lyykmDHqr35nQ_1ZGVKqX60jdQukUNScsDMwzmWx_jM";

/**
 * Función principal para recibir peticiones POST
 */
function doPost(e) {
  try {
    const params = JSON.parse(e.postData.contents);
    const action = params.action;
    let response;

    switch (action) {
      case 'init': response = initDB(); break;
      case 'login': response = handleLogin(params.data); break;
      case 'saveSummary': response = saveSummary(params.data); break;
      case 'getStats': response = getHistoricalData(); break;
      case 'createUser': response = createUser(params.data); break;
      case 'getUsers': response = getUsers(); break;
      case 'updateUser': response = updateUser(params.data); break;
      case 'deleteUser': response = deleteUser(params.data.id); break;
      case 'updateProfile': response = updateProfile(params.data); break;
      case 'changePassword': response = changePassword(params.data); break;
      case 'forgotPassword': response = forgotPassword(params.data.email); break;
      case 'resetPassword': response = resetPassword(params.data.token, params.data.pass); break;
      default: throw new Error('Acción no reconocida');
    }

    return ContentService.createTextOutput(JSON.stringify({ success: true, data: response }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ success: false, error: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

/**
 * Inicializa las hojas necesarias con la nueva estructura
 */
function initDB() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  
  // ESTRUCTURA DE USUARIOS: 
  // 0:ID, 1:Nombre, 2:Email, 3:Password_Hash, 4:Estado, 5:Token, 6:Expiration, 
  // 7:Rol, 8:Ultimo_Acceso, 9:Fecha_Creacion, 10:Intentos_Fallidos, 11:Bloqueado_Hasta,
  // 12:Recovery_Token, 13:Recovery_Expiration
  const headers = ["ID", "Nombre", "Email", "Password_Hash", "Estado", "Token", "Expiration", "Rol", "Ultimo_Acceso", "Fecha_Creacion", "Intentos_Fallidos", "Bloqueado_Hasta", "Recovery_Token", "Recovery_Expiration"];

  let sheet = ss.getSheetByName("Usuarios");
  if (!sheet) {
    sheet = ss.insertSheet("Usuarios");
    sheet.appendRow(headers);
    // Insertar Usuario Predeterminado (Admin)
    sheet.appendRow([
      "admin_01", 
      "Admin Sistema", 
      "hader189@gmail.com", 
      "PuYRxl+rpz8XbqI8EfaG91sHCYAlEQkPYcxT7HF6UjA=", // Hash de Excol123**
      "active",
      "",
      "",
      "Admin",
      "",
      new Date(),
      0,
      "",
      "",
      ""
    ]);
  } else {
    // Si ya existe, nos aseguramos de que tenga todas las columnas
    const currentHeaders = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    if (currentHeaders.length < headers.length) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    }
  }
  sheet.getRange(1, 1, 1, headers.length).setBackground("#1a1f2e").setFontColor("#00e5ff").setFontWeight("bold");

  if (!ss.getSheetByName("Historial")) {
    const sheetH = ss.insertSheet("Historial");
    sheetH.appendRow(["Fecha", "Mes", "Año", "Total_DIAN", "Total_Siesa", "Total_Faltantes", "Accuracy_Pct"]);
    sheetH.getRange(1, 1, 1, 7).setBackground("#1a1f2e").setFontColor("#00e5ff").setFontWeight("bold");
  }

  return "Base de datos actualizada correctamente";
}

/**
 * Maneja el login con soporte para SHA-256
 */
function handleLogin(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  const inputHash = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, data.pass));

  for (let i = 1; i < values.length; i++) {
    const dbEmail = values[i][2];
    const dbPass = values[i][3];
    const dbStatus = values[i][4];
    const dbRole = values[i][7] || "Operador";
    const dbFailCount = parseInt(values[i][10] || 0);
    const dbLockUntil = values[i][11];

    if (dbEmail === data.user || values[i][0] === data.user) {
      // Verificar Bloqueo
      if (dbLockUntil && new Date().getTime() < new Date(dbLockUntil).getTime()) {
        throw new Error("CUENTA BLOQUEADA TEMPORALMENTE POR SEGURIDAD. INTENTA MÁS TARDE.");
      }

      if (dbStatus === "archived") throw new Error("ESTA CUENTA HA SIDO DESACTIVADA.");
      if (dbStatus === "pending") throw new Error("CUENTA PENDIENTE DE ACTIVACIÓN. REVISA TU CORREO.");

      if (dbPass === inputHash) {
        // Login Exitoso: Resetear intentos y actualizar último acceso
        sheet.getRange(i + 1, 9, 1, 4).setValues([[new Date(), "", 0, ""]]);
        
        return {
          id: values[i][0],
          name: values[i][1],
          email: dbEmail,
          status: dbStatus,
          role: dbRole
        };
      } else {
        // Login Fallido: Incrementar contador
        const newFailCount = dbFailCount + 1;
        let lockTime = "";
        if (newFailCount >= 5) {
          lockTime = new Date(new Date().getTime() + (30 * 60 * 1000)); // 30 min bloqueo
        }
        sheet.getRange(i + 1, 11, 1, 2).setValues([[newFailCount, lockTime]]);
        
        if (newFailCount >= 5) throw new Error("DEMASIADOS INTENTOS FALLIDOS. CUENTA BLOQUEADA POR 30 MIN.");
        throw new Error(`CONTRASEÑA INCORRECTA. INTENTOS RESTANTES: ${5 - newFailCount}`);
      }
    }
  }
  throw new Error('EL USUARIO NO EXISTE EN EL SISTEMA');
}

/**
 * SISTEMA DE INVITACIÓN POR CORREO
 */
function createUser(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const rows = sheet.getDataRange().getValues();
  
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][2] === data.email) return { success: false, error: "EL EMAIL YA ESTÁ REGISTRADO" };
  }

  const userId = "user_" + Math.random().toString(36).substr(2, 5);
  const token = Utilities.getUuid();
  const expiration = new Date().getTime() + (48 * 60 * 60 * 1000); // 48 horas según requerimiento

  // Columnas: ID, Nombre, Email, PassHash, Estado, Token, Exp, Rol, LastAcc, CreatedAt, Fails, Lock, RecToken, RecExp
  sheet.appendRow([
    userId, 
    data.name, 
    data.email, 
    "", 
    "pending", 
    token, 
    expiration, 
    data.role || "Operador", 
    "", 
    new Date(), 
    0, 
    "", 
    "", 
    ""
  ]);
  
  sendInvitationEmail(data.name, data.email, token);
  return { success: true, userId: userId };
}

function sendInvitationEmail(name, email, token) {
  const scriptUrl = ScriptApp.getService().getUrl();
  const invitationLink = `${scriptUrl}?token=${token}`;
  const appUrl = "https://comparador-facturas.vercel.app/";
  
  const htmlBody = `
    <div style="font-family: sans-serif; padding: 20px; border: 1px solid #2a3050; background-color: #0f1117; color: #c8d0e0;">
      <h2 style="color: #00e5ff;">Bienvenido al Sistema</h2>
      <p>Hola <strong>${name}</strong>, se ha creado una cuenta para ti en el <strong>Comparador de Facturas</strong>.</p>
      <p>Haz clic abajo para configurar tu contraseña (válido por 24 horas):</p>
      <a href="${invitationLink}" style="display: inline-block; padding: 12px 24px; background-color: #00e5ff; color: #0a0e1a; text-decoration: none; border-radius: 4px; font-weight: bold;">CONFIGURAR MI CONTRASEÑA</a>
      <hr style="border-color: #2a3050; margin: 24px 0;">
      <p>Una vez activada tu cuenta, podrás ingresar al sistema en:</p>
      <a href="${appUrl}" style="display: inline-block; padding: 10px 20px; background-color: #1a1f2e; color: #00e5ff; text-decoration: none; border-radius: 4px; border: 1px solid #00e5ff; font-weight: bold;">${appUrl}</a>
      <p style="font-size:0.7rem; color:#5a6480; margin-top:20px;">Si no solicitaste esta cuenta, ignora este correo.</p>
    </div>`;

  GmailApp.sendEmail(email, "Activa tu cuenta - Comparador Facturas", "", { htmlBody: htmlBody });
}

function doGet(e) {
  const token = e.parameter.token;
  const recoveryToken = e.parameter.recoveryToken;

  if (!token && !recoveryToken) return HtmlService.createHtmlOutput("<h1 style='background:#0f1117; color:white; height:100vh; display:flex; align-items:center; justify-content:center; margin:0;'>ACCESO DENEGADO</h1>");

  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const data = sheet.getDataRange().getValues();
  
  let userFound = null;
  let isRecovery = false;

  if (token) {
    for (let i = 1; i < data.length; i++) {
      if (data[i][5] === token && new Date().getTime() < data[i][6]) {
        userFound = { name: data[i][1], token: token };
        break;
      }
    }
  } else if (recoveryToken) {
    for (let i = 1; i < data.length; i++) {
      if (data[i][12] === recoveryToken && new Date().getTime() < data[i][13]) {
        userFound = { name: data[i][1], token: recoveryToken };
        isRecovery = true;
        break;
      }
    }
  }

  if (!userFound) return HtmlService.createHtmlOutput("<body style='background:#0f1117; color:#ff5252; display:flex; align-items:center; justify-content:center; height:100vh;'><h2>El enlace ha expirado o no es válido.</h2></body>");

  const template = `
    <body style="background:#0f1117; color:#c8d0e0; font-family:sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0;">
      <style>
        .btn-loader { width: 14px; aspect-ratio: 1; display: grid; animation: l14 4s infinite; }
        .btn-loader::before, .btn-loader::after { content: ""; grid-area: 1/1; border: 2px solid; border-radius: 50%; border-color: #00e5ff #00e5ff #0000 #0000; animation: l14 1s infinite linear; }
        .btn-loader::after { border-color: #0000 #0000 #00e676 #00e676; animation-direction: reverse; }
        @keyframes l14{ 100%{transform: rotate(1turn)} }
      </style>
      <div style="background:#1a1f2e; padding:30px; border:1px solid #2a3050; border-radius:8px; width:320px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
        <h3 style="color:#00e5ff; margin-top:0;">Hola ${userFound.name}</h3>
        <p style="font-size:0.85em; color:#8892aa;">Define tu contraseña para activar la cuenta</p>
        
        <div style="position:relative; margin-bottom:10px;">
          <input type="password" id="p1" placeholder="Nueva Contraseña" style="width:100%; box-sizing:border-box; padding:12px; background:#0a0d14; border:1px solid #2a3050; color:white; border-radius:4px;">
          <div onclick="t('p1')" style="position:absolute; right:10px; top:50%; transform:translateY(-50%); cursor:pointer; color:#5a6480;">👁</div>
        </div>

        <div style="position:relative; margin-bottom:20px;">
          <input type="password" id="p2" placeholder="Confirmar Contraseña" style="width:100%; box-sizing:border-box; padding:12px; background:#0a0d14; border:1px solid #2a3050; color:white; border-radius:4px;">
          <div onclick="t('p2')" style="position:absolute; right:10px; top:50%; transform:translateY(-50%); cursor:pointer; color:#5a6480;">👁</div>
        </div>

        <button id="btn-save" onclick="save()" style="width:100%; padding:12px; background:#00e5ff; color:#0a0e1a; border:none; border-radius:4px; font-weight:bold; cursor:pointer; text-transform:uppercase; letter-spacing:1px; display:flex; align-items:center; justify-content:center; gap:8px;">ACTIVAR MI CUENTA</button>
        <p id="msg" style="font-size:0.75em; margin-top:15px; color:#ff5252; font-weight:bold;"></p>
      </div>
      <script>
        function t(id) {
          const x = document.getElementById(id);
          x.type = x.type === 'password' ? 'text' : 'password';
        }
        function save() {
          const p1 = document.getElementById('p1').value;
          const p2 = document.getElementById('p2').value;
          const btn = document.getElementById('btn-save');
          const msg = document.getElementById('msg');
          if(p1 !== p2) { msg.innerText = "LAS CONTRASEÑAS NO COINCIDEN"; return; }
          if(p1.length < 8) { msg.innerText = "MÍNIMO 8 CARACTERES"; return; }
          if(!/[A-Z]/.test(p1) || !/[0-9]/.test(p1)) { msg.innerText = "REQUIERE UNA MAYÚSCULA Y UN NÚMERO"; return; }
          
          btn.disabled = true;
          btn.innerHTML = '<div class="btn-loader"></div> PROCESANDO...';
          msg.style.color = "#00e5ff";
          msg.innerText = "";
          
          const action = ${isRecovery ? "'resetPassword'" : "'setPassword'"} ;
          const successMsg = ${isRecovery ? "'¡CONTRASEÑA RESTABLECIDA!'" : "'¡CUENTA ACTIVADA!'"};

          google.script.run.withSuccessHandler((res) => {
            if(res && res.success === false) {
               msg.style.color = "#ff5252";
               msg.innerText = res.error || "ERROR EN EL PROCESO";
               btn.disabled = false;
               btn.innerText = "REINTENTAR";
               return;
            }
            document.body.innerHTML = "<div style='text-align:center; font-family:sans-serif; background:#0f1117; height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; margin:0;'><h2 style='color:#00e676'>" + successMsg + " 🎉</h2><p style='color:#8892aa; margin-bottom:24px;'>Tu contraseña ha sido configurada exitosamente.</p><p style='color:#c8d0e0; margin-bottom:16px;'>Ya puedes ingresar al sistema:</p><a href='https://comparador-facturas.vercel.app/' style='display:inline-block; padding:12px 28px; background:#00e5ff; color:#0a0e1a; text-decoration:none; border-radius:4px; font-weight:bold; letter-spacing:1px; font-size:1rem;'>IR AL SISTEMA →</a></div>";
          })[isRecovery ? "resetPassword" : "setPassword"]("${userFound.token}", p1);
        }
      </script>
    </body>`;
  return HtmlService.createHtmlOutput(template).setTitle(isRecovery ? "Recuperar Cuenta" : "Activar Cuenta").setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function setPassword(token, password) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const data = sheet.getDataRange().getValues();
  for (let i = 1; i < data.length; i++) {
    if (data[i][5] === token) {
      const passHash = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, password));
      // Resetear token y activar cuenta
      sheet.getRange(i + 1, 4, 1, 4).setValues([[passHash, "active", "", ""]]);
      return true;
    }
  }
  return false;
}

/**
 * FUNCIONES DE ADMINISTRACIÓN
 */
function getUsers() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  return values.slice(1).map(row => ({
    id: row[0],
    name: row[1],
    email: row[2],
    status: row[4],
    role: row[7],
    lastLogin: row[8],
    createdAt: row[9]
  }));
}

function updateUser(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  for (let i = 1; i < values.length; i++) {
    if (values[i][0] === data.id) {
      if (data.status) sheet.getRange(i + 1, 5).setValue(data.status);
      if (data.role) sheet.getRange(i + 1, 8).setValue(data.role);
      return { success: true };
    }
  }
  throw new Error("Usuario no encontrado");
}

function deleteUser(id) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  for (let i = 1; i < values.length; i++) {
    if (values[i][0] === id) {
      sheet.deleteRow(i + 1);
      return { success: true };
    }
  }
  throw new Error("Usuario no encontrado");
}

/**
 * FUNCIONES DE PERFIL
 */
function updateProfile(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  for (let i = 1; i < values.length; i++) {
    if (values[i][0] === data.id) {
      sheet.getRange(i + 1, 2).setValue(data.name);
      return { success: true, name: data.name };
    }
  }
  throw new Error("Usuario no encontrado");
}

function changePassword(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  const oldHash = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, data.oldPass));
  const newHash = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, data.newPass));

  for (let i = 1; i < values.length; i++) {
    if (values[i][0] === data.id) {
      if (values[i][3] !== oldHash) throw new Error("LA CONTRASEÑA ACTUAL ES INCORRECTA");
      sheet.getRange(i + 1, 4).setValue(newHash);
      return { success: true };
    }
  }
  throw new Error("Usuario no encontrado");
}

/**
 * RECUPERACIÓN DE CONTRASEÑA
 */
function forgotPassword(email) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  for (let i = 1; i < values.length; i++) {
    if (values[i][2] === email) {
      if (values[i][4] !== "active") throw new Error("LA CUENTA NO ESTÁ ACTIVA.");
      
      const token = Utilities.getUuid();
      const exp = new Date().getTime() + (1 * 60 * 60 * 1000); // 1 hora
      sheet.getRange(i + 1, 13, 1, 2).setValues([[token, exp]]);
      
      sendRecoveryEmail(values[i][1], email, token);
      return { success: true };
    }
  }
  throw new Error("EL CORREO NO ESTÁ REGISTRADO.");
}

function sendRecoveryEmail(name, email, token) {
  const scriptUrl = ScriptApp.getService().getUrl();
  const recoveryLink = `${scriptUrl}?recoveryToken=${token}`;
  
  const htmlBody = `
    <div style="font-family: sans-serif; padding: 20px; border: 1px solid #2a3050; background-color: #0f1117; color: #c8d0e0;">
      <h2 style="color: #00e5ff;">Recuperación de Contraseña</h2>
      <p>Hola <strong>${name}</strong>, has solicitado restablecer tu contraseña.</p>
      <p>Haz clic abajo para definir una nueva (válido por 1 hora):</p>
      <a href="${recoveryLink}" style="display: inline-block; padding: 12px 24px; background-color: #00e5ff; color: #0a0e1a; text-decoration: none; border-radius: 4px; font-weight: bold;">RESTABLECER CONTRASEÑA</a>
      <p style="font-size:0.7rem; color:#5a6480; margin-top:20px;">Si no solicitaste esto, puedes ignorar este correo.</p>
    </div>`;

  GmailApp.sendEmail(email, "Recupera tu contraseña - Comparador Facturas", "", { htmlBody: htmlBody });
}

function resetPassword(token, newPass) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Usuarios");
  const values = sheet.getDataRange().getValues();
  
  for (let i = 1; i < values.length; i++) {
    if (values[i][12] === token) {
      if (new Date().getTime() > values[i][13]) throw new Error("EL TOKEN HA EXPIRADO.");
      
      const newHash = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, newPass));
      // Actualizar pass y limpiar recovery tokens
      sheet.getRange(i + 1, 4).setValue(newHash);
      sheet.getRange(i + 1, 13, 1, 2).setValues([["", ""]]);
      return { success: true };
    }
  }
  throw new Error("TOKEN NO VÁLIDO.");
}

/**
 * Guarda el resumen de una comparación
 */
function saveSummary(data) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Historial");
  const values = sheet.getDataRange().getValues();
  const now = new Date();
  const todayStr = Utilities.formatDate(now, "GMT-5", "yyyy-MM-dd");
  
  // Buscar duplicados por fecha (Columna A)
  let duplicates = 0;
  for (let i = 1; i < values.length; i++) {
    const rowDate = values[i][0];
    if (rowDate instanceof Date) {
      const rowDateStr = Utilities.formatDate(rowDate, "GMT-5", "yyyy-MM-dd");
      if (rowDateStr === todayStr) duplicates++;
    }
  }

  // Si hay duplicados y no viene forzado, advertir
  if (duplicates > 0 && !data.force) {
    return {
      success: true,
      warning: true,
      message: "Ya existen registros con esta fecha.",
      duplicates: duplicates
    };
  }

  const meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];
  sheet.appendRow([now, meses[now.getMonth()], now.getFullYear(), data.total_dian, data.total_en_siesa, data.total_faltantes, data.porcentaje_completitud]);
  return { success: true, message: "Resumen guardado correctamente" };
}

/**
 * Obtiene datos históricos
 */
function getHistoricalData() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = ss.getSheetByName("Historial");
  const values = sheet.getDataRange().getValues();
  const data = values.slice(1).map(row => ({
    fecha: row[0], mes: row[1], anio: row[2], dian: row[3], siesa: row[4], faltantes: row[5], accuracy: row[6]
  }));
  return data.slice(-12);
}
