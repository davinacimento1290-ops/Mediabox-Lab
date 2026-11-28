package com.mediabox.videohub;

import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.content.ContentValues;
import android.webkit.CookieManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.VideoView;
import android.widget.MediaController;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FloatingOverlayService extends Service {
    public static final String ACTION_STOP = "com.mediabox.videohub.FLOATING_STOP";
    private static final String BASE_URL = "https://mediabox-lab-staging.up.railway.app";
    private static final String CHANNEL_ID = "mediabox_floating_downloader";
    private static final int NOTIFICATION_ID = 4103;

    private WindowManager windowManager;
    private TextView bubble;
    private View panel;
    private WindowManager.LayoutParams bubbleParams;
    private EditText linkInput;
    private TextView statusText;
    private TextView titleText;
    private ProgressBar progressBar;
    private Button previewButton;
    private Button downloadButton;
    private VideoView videoPreview;
    private boolean panelOpen;
    private volatile boolean downloadRunning;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final Runnable pollStatus = new Runnable() {
        @Override public void run() {
            if (!downloadRunning) return;
            network.execute(() -> {
                try {
                    JSONObject state = requestJson("GET", "/status", null);
                    handler.post(() -> updateDownloadState(state));
                } catch (Exception e) {
                    handler.post(() -> setStatus("Reconectando ao servidor..."));
                }
            });
            if (downloadRunning) handler.postDelayed(this, 1200);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        createNotificationChannel();
        startForeground(NOTIFICATION_ID, buildNotification());
        showBubble();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "MEDIABOX Downloader Flutuante", NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Mantém a bolinha do MEDIABOX ativa quando o app está fechado.");
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) manager.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification() {
        Intent stop = new Intent(this, FloatingOverlayService.class);
        stop.setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(
            this, 4104, stop,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
            ? new Notification.Builder(this, CHANNEL_ID) : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("MEDIABOX Downloader")
            .setContentText("Downloader flutuante ativo.")
            .setOngoing(true)
            .addAction(new Notification.Action.Builder(
                android.R.drawable.ic_menu_close_clear_cancel, "Desativar", stopPending
            ).build());
        return builder.build();
    }

    private int dp(float value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private GradientDrawable background(int color, int stroke, int radius) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color);
        d.setCornerRadius(dp(radius));
        d.setStroke(dp(1), stroke);
        return d;
    }

    private TextView text(String value, int size, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(value);
        t.setTextSize(size);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return t;
    }

    private Button button(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(13);
        b.setTextColor(Color.WHITE);
        b.setBackground(background(Color.rgb(22, 48, 78), Color.rgb(53, 115, 157), 12));
        return b;
    }

    private void showBubble() {
        if (bubble != null || !android.provider.Settings.canDrawOverlays(this)) return;
        bubble = new TextView(this);
        bubble.setText("M");
        bubble.setTextColor(Color.rgb(103, 232, 249));
        bubble.setTextSize(21);
        bubble.setGravity(Gravity.CENTER);
        bubble.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        GradientDrawable bg = new GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            new int[]{Color.rgb(22, 49, 83), Color.rgb(25, 17, 55)}
        );
        bg.setShape(GradientDrawable.OVAL);
        bg.setStroke(dp(2), Color.rgb(72, 205, 255));
        bubble.setBackground(bg);
        bubble.setElevation(dp(8));

        bubbleParams = new WindowManager.LayoutParams(
            dp(58), dp(58),
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        );
        bubbleParams.gravity = Gravity.TOP | Gravity.START;
        bubbleParams.x = dp(14);
        bubbleParams.y = getResources().getDisplayMetrics().heightPixels / 3;
        bubble.setOnTouchListener(new View.OnTouchListener() {
            private int startX, startY;
            private float downX, downY;
            private boolean moved;
            @Override public boolean onTouch(View v, MotionEvent e) {
                switch (e.getAction()) {
                    case MotionEvent.ACTION_DOWN:
                        startX = bubbleParams.x; startY = bubbleParams.y;
                        downX = e.getRawX(); downY = e.getRawY(); moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int dx = (int)(e.getRawX() - downX), dy = (int)(e.getRawY() - downY);
                        if (Math.abs(dx) > dp(4) || Math.abs(dy) > dp(4)) moved = true;
                        bubbleParams.x = startX + dx; bubbleParams.y = startY + dy;
                        try { windowManager.updateViewLayout(bubble, bubbleParams); } catch (Exception ignored) { }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!moved) openPanel();
                        return true;
                    default: return true;
                }
            }
        });
        try { windowManager.addView(bubble, bubbleParams); }
        catch (Exception e) { bubble = null; stopSelf(); }
    }

    private void openPanel() {
        if (panelOpen || !android.provider.Settings.canDrawOverlays(this)) return;
        panelOpen = true;

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(14), dp(12), dp(14), dp(14));
        card.setBackground(background(Color.rgb(7, 13, 25), Color.rgb(56, 189, 248), 20));

        LinearLayout header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        TextView brand = text("MEDIABOX", 17, Color.rgb(103, 232, 249), true);
        header.addView(brand, new LinearLayout.LayoutParams(0, dp(42), 1));

        Button minimize = button("—");
        minimize.setMinWidth(dp(42));
        minimize.setOnClickListener(v -> closePanel());
        header.addView(minimize, new LinearLayout.LayoutParams(dp(46), dp(42)));
        Button close = button("×");
        close.setMinWidth(dp(42));
        close.setOnClickListener(v -> closePanel());
        header.addView(close, new LinearLayout.LayoutParams(dp(46), dp(42)));
        card.addView(header);

        TextView subtitle = text("DOWNLOADER FLUTUANTE", 10, Color.rgb(145, 166, 194), true);
        LinearLayout.LayoutParams subParams = new LinearLayout.LayoutParams(-1, dp(24));
        card.addView(subtitle, subParams);

        linkInput = new EditText(this);
        linkInput.setSingleLine(true);
        linkInput.setTextSize(14);
        linkInput.setTextColor(Color.WHITE);
        linkInput.setHintTextColor(Color.rgb(130, 148, 173));
        linkInput.setHint("Cole o link do vídeo aqui");
        linkInput.setPadding(dp(12), 0, dp(12), 0);
        linkInput.setSelectAllOnFocus(false);
        linkInput.setBackground(background(Color.rgb(14, 27, 45), Color.rgb(45, 66, 94), 12));
        LinearLayout.LayoutParams inputParams = new LinearLayout.LayoutParams(-1, dp(52));
        inputParams.topMargin = dp(8);
        card.addView(linkInput, inputParams);

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        previewButton = button("Prévia");
        downloadButton = button("Baixar vídeo");
        LinearLayout.LayoutParams actionParams = new LinearLayout.LayoutParams(0, dp(48), 1);
        actionParams.topMargin = dp(10);
        actionParams.rightMargin = dp(5);
        actions.addView(previewButton, actionParams);
        LinearLayout.LayoutParams downloadParams = new LinearLayout.LayoutParams(0, dp(48), 1);
        downloadParams.topMargin = dp(10);
        downloadParams.leftMargin = dp(5);
        actions.addView(downloadButton, downloadParams);
        card.addView(actions);

        titleText = text("Prévia do vídeo aparecerá aqui", 12, Color.rgb(190, 205, 225), true);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(-1, dp(34));
        titleParams.topMargin = dp(10);
        card.addView(titleText, titleParams);

        videoPreview = new VideoView(this);
        videoPreview.setBackgroundColor(Color.BLACK);
        LinearLayout.LayoutParams videoParams = new LinearLayout.LayoutParams(-1, dp(174));
        videoParams.topMargin = dp(4);
        card.addView(videoPreview, videoParams);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgress(0);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(-1, dp(8));
        progressParams.topMargin = dp(12);
        card.addView(progressBar, progressParams);

        statusText = text("Cole um link para começar.", 11, Color.rgb(145, 166, 194), false);
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(-1, dp(42));
        statusParams.topMargin = dp(5);
        card.addView(statusText, statusParams);

        previewButton.setOnClickListener(v -> carregarPrevia());
        downloadButton.setOnClickListener(v -> iniciarFluxoDownload());

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(true);
        scroll.setVerticalScrollBarEnabled(true);
        scroll.addView(card, new ScrollView.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        panel = scroll;
        int width = Math.min(getResources().getDisplayMetrics().widthPixels - dp(20), dp(430));
        int availableHeight = getResources().getDisplayMetrics().heightPixels - dp(100);
        int height = Math.min(Math.max(dp(300), availableHeight), dp(620));
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
            width, height,
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY : WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        );
        params.gravity = Gravity.CENTER;
        params.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE;
        try { windowManager.addView(panel, params); }
        catch (Exception e) { panelOpen = false; panel = null; }
    }

    private void carregarPrevia() {
        String link = linkInput == null ? "" : linkInput.getText().toString().trim();
        if (link.isEmpty()) { setStatus("Cole o link do vídeo primeiro."); return; }
        setBusy(true, "Buscando prévia no servidor...");
        network.execute(() -> {
            try {
                JSONObject payload = new JSONObject();
                payload.put("url", link);
                JSONObject result = requestJson("POST", "/preview/link", payload);
                String media = result.optString("media_url", "");
                String title = result.optString("title", "Vídeo");
                if (media.isEmpty()) throw new Exception("O servidor não retornou uma prévia reproduzível.");
                String mediaUrl = media.startsWith("http") ? media : BASE_URL + media;
                handler.post(() -> {
                    if (!panelOpen || videoPreview == null) return;
                    titleText.setText(title);
                    MediaController controls = new MediaController(this);
                    controls.setAnchorView(videoPreview);
                    videoPreview.setMediaController(controls);
                    videoPreview.setOnPreparedListener(mp -> {
                        mp.setVolume(0f, 0f);
                        int videoWidth = mp.getVideoWidth();
                        int videoHeight = mp.getVideoHeight();
                        if (videoWidth > 0 && videoHeight > 0) {
                            int availableWidth = Math.max(dp(180),
                                getResources().getDisplayMetrics().widthPixels - dp(64));
                            int targetHeight = Math.round(availableWidth * (videoHeight / (float) videoWidth));
                            targetHeight = Math.max(dp(110), Math.min(dp(230), targetHeight));
                            LinearLayout.LayoutParams videoParams =
                                (LinearLayout.LayoutParams) videoPreview.getLayoutParams();
                            videoParams.height = targetHeight;
                            videoPreview.setLayoutParams(videoParams);
                        }
                        videoPreview.start();
                        setBusy(false, "Prévia carregada. Confira antes de baixar.");
                    });
                    videoPreview.setOnErrorListener((mp, what, extra) -> {
                        setBusy(false, "Não foi possível reproduzir esta prévia. Tente baixar o vídeo diretamente.");
                        return true;
                    });
                    videoPreview.setVideoURI(Uri.parse(mediaUrl));
                });
            } catch (Exception e) {
                handler.post(() -> setBusy(false, e.getMessage()));
            }
        });
    }

    private void iniciarFluxoDownload() {
        String link = linkInput == null ? "" : linkInput.getText().toString().trim();
        if (link.isEmpty()) { setStatus("Cole um link válido primeiro."); return; }
        if (downloadRunning) { setStatus("Já existe um download em andamento."); return; }
        setBusy(true, "Enviando vídeo ao MEDIABOX...");
        network.execute(() -> {
            try {
                JSONObject payload = new JSONObject();
                payload.put("url", link);
                payload.put("plataforma", "auto");
                payload.put("formato", "mp4");
                payload.put("marca_dagua", false);
                payload.put("marca_tipo", "nao");
                payload.put("posicao_marca", "inferior_direito");
                requestJson("POST", "/start", payload);
                downloadRunning = true;
                handler.post(() -> {
                    setBusy(false, "Download iniciado no servidor.");
                    if (progressBar != null) progressBar.setProgress(0);
                    handler.removeCallbacks(pollStatus);
                    handler.post(pollStatus);
                });
            } catch (Exception e) {
                handler.post(() -> setBusy(false,
                    e.getMessage() == null ? "Não foi possível iniciar o download." : e.getMessage()));
            }
        });
    }

    private JSONObject requestJson(String method, String path, JSONObject body) throws Exception {
        HttpURLConnection connection = null;
        try {
            URL url = new URL(BASE_URL + path);
            connection = (HttpURLConnection) url.openConnection();
            connection.setRequestMethod(method);
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(30000);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("User-Agent", "MEDIABOX-Android/1.0");
            String cookie = CookieManager.getInstance().getCookie(BASE_URL);
            if (cookie != null && !cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                byte[] bytes = body.toString().getBytes("UTF-8");
                try (OutputStream out = connection.getOutputStream()) { out.write(bytes); }
            }
            int code = connection.getResponseCode();
            InputStream stream = code >= 200 && code < 400 ? connection.getInputStream() : connection.getErrorStream();
            StringBuilder raw = new StringBuilder();
            if (stream != null) {
                try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, "UTF-8"))) {
                    String line;
                    while ((line = reader.readLine()) != null) raw.append(line);
                }
            }
            JSONObject result = raw.length() == 0 ? new JSONObject() : new JSONObject(raw.toString());
            if (code < 200 || code >= 300) {
                String error = result.optString("erro", "O servidor respondeu HTTP " + code + ".");
                throw new Exception(error);
            }
            return result;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void updateDownloadState(JSONObject state) {
        int percent = state.optInt("progresso", 0);
        String message = state.optString("status", "Processando vídeo...");
        if (panelOpen && progressBar != null) {
            progressBar.setProgress(Math.max(0, Math.min(100, percent)));
        }
        if (panelOpen && statusText != null) {
            statusText.setText(message + "  " + percent + "%");
        }
        if (state.optBoolean("ativo", false)) return;
        String error = state.optString("erro", "");
        String downloadUrl = state.optString("download_url", "");
        if (!error.isEmpty()) {
            downloadRunning = false;
            handler.removeCallbacks(pollStatus);
            setBusy(false, error);
            return;
        }
        if (!downloadUrl.isEmpty()) {
            downloadRunning = false;
            handler.removeCallbacks(pollStatus);
            setBusy(false, "Vídeo pronto. Salvando em Downloads...");
            try {
                String finalUrl = downloadUrl.startsWith("http") ? downloadUrl : BASE_URL + downloadUrl;
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(finalUrl));
                String filename = state.optString("arquivo", "mediabox_video.mp4");
                request.setTitle(filename);
                request.setDescription("Download concluído pelo servidor MEDIABOX");
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                request.setAllowedOverMetered(true);
                request.setAllowedOverRoaming(false);
                request.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_MOVIES, filename);
                DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                if (manager == null) throw new IllegalStateException("Gerenciador de downloads indisponível.");
                manager.enqueue(request);
                setStatus("Download enviado para a pasta Downloads. 🟢");
                NotificationManager notifications =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                if (notifications != null) {
                    Notification.Builder notice = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                        ? new Notification.Builder(this, CHANNEL_ID)
                        : new Notification.Builder(this);
                    notice.setSmallIcon(android.R.drawable.stat_sys_download_done)
                        .setContentTitle("MEDIABOX: vídeo pronto")
                        .setContentText("O arquivo foi enviado para Downloads.")
                        .setAutoCancel(true);
                    notifications.notify(NOTIFICATION_ID, notice.build());
                }
            } catch (Exception e) {
                setStatus("Vídeo pronto, mas não foi possível salvar: " + e.getMessage());
            }
        } else {
            downloadRunning = false;
            setBusy(false, message);
        }
    }

    private void setBusy(boolean busy, String message) {
        if (statusText != null) statusText.setText(message);
        if (previewButton != null) previewButton.setEnabled(!busy);
        if (downloadButton != null) downloadButton.setEnabled(!busy && !downloadRunning);
    }

    private void setStatus(String message) {
        if (statusText != null) statusText.setText(message);
    }

    private void closePanel() {
        if (videoPreview != null) {
            videoPreview.stopPlayback();
            videoPreview = null;
        }
        if (panel != null) {
            try { windowManager.removeView(panel); } catch (Exception ignored) { }
            panel = null;
        }
        panelOpen = false;
        linkInput = null; statusText = null; titleText = null;
        progressBar = null; previewButton = null; downloadButton = null;
    }

    @Override public void onDestroy() {
        closePanel();
        if (bubble != null) {
            try { windowManager.removeView(bubble); } catch (Exception ignored) { }
            bubble = null;
        }
        network.shutdownNow();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
