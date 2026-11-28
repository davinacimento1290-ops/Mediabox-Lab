package com.mediabox.videohub;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public class FloatingOverlayService extends Service {
    public static final String ACTION_STOP = "com.mediabox.videohub.FLOATING_STOP";
    private static final String HOME_URL = "https://mediabox-lab-staging.up.railway.app/";
    private static final String APP_HOST = "mediabox-lab-staging.up.railway.app";
    private static final String CHANNEL_ID = "mediabox_floating_downloader";
    private static final int NOTIFICATION_ID = 4103;

    private WindowManager windowManager;
    private TextView bubble;
    private View panel;
    private WebView panelWebView;
    private WindowManager.LayoutParams bubbleParams;
    private boolean panelOpen;

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
                CHANNEL_ID, "MEDIABOX Floating Downloader", NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Mantém a bolinha flutuante ativa quando o MEDIABOX está fechado.");
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
            ? new Notification.Builder(this, CHANNEL_ID)
            : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("MEDIABOX Floating Downloader")
            .setContentText("Bolinha flutuante ativa. Toque para abrir o painel.")
            .setOngoing(true)
            .addAction(new Notification.Action.Builder(
                android.R.drawable.ic_menu_close_clear_cancel, "Desativar bolinha", stopPending
            ).build());
        return builder.build();
    }

    private int dp(float value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private GradientDrawable roundedBackground(int color, int strokeColor, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), strokeColor);
        return drawable;
    }

    private void showBubble() {
        if (bubble != null || !android.provider.Settings.canDrawOverlays(this)) return;
        bubble = new TextView(this);
        bubble.setText("M");
        bubble.setTextColor(Color.rgb(103, 232, 249));
        bubble.setTextSize(21);
        bubble.setGravity(Gravity.CENTER);
        bubble.setTypeface(null, android.graphics.Typeface.BOLD);
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
            @Override
            public boolean onTouch(View view, MotionEvent event) {
                switch (event.getAction()) {
                    case MotionEvent.ACTION_DOWN:
                        startX = bubbleParams.x;
                        startY = bubbleParams.y;
                        downX = event.getRawX();
                        downY = event.getRawY();
                        moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int dx = (int) (event.getRawX() - downX);
                        int dy = (int) (event.getRawY() - downY);
                        if (Math.abs(dx) > dp(4) || Math.abs(dy) > dp(4)) moved = true;
                        bubbleParams.x = startX + dx;
                        bubbleParams.y = startY + dy;
                        try { windowManager.updateViewLayout(bubble, bubbleParams); } catch (Exception ignored) { }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!moved) openPanel();
                        return true;
                    default:
                        return true;
                }
            }
        });
        try {
            windowManager.addView(bubble, bubbleParams);
        } catch (Exception e) {
            bubble = null;
            stopSelf();
        }
    }

    private void openPanel() {
        if (panelOpen || !android.provider.Settings.canDrawOverlays(this)) return;
        panelOpen = true;

        LinearLayout container = new LinearLayout(this);
        container.setOrientation(LinearLayout.VERTICAL);
        container.setBackground(roundedBackground(Color.rgb(7, 13, 25), Color.rgb(56, 189, 248), 18));
        container.setClipToOutline(true);

        LinearLayout header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(14), dp(8), dp(8), dp(8));
        header.setBackgroundColor(Color.rgb(16, 31, 52));

        TextView title = new TextView(this);
        title.setText("MEDIABOX  •  FLOATING");
        title.setTextColor(Color.rgb(103, 232, 249));
        title.setTextSize(13);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        header.addView(title, new LinearLayout.LayoutParams(0, dp(42), 1));

        Button minimize = new Button(this);
        minimize.setText("—");
        minimize.setTextSize(16);
        minimize.setMinWidth(dp(42));
        minimize.setPadding(0, 0, 0, 0);
        minimize.setOnClickListener(v -> closePanel());
        header.addView(minimize, new LinearLayout.LayoutParams(dp(48), dp(42)));

        Button close = new Button(this);
        close.setText("×");
        close.setTextSize(19);
        close.setMinWidth(dp(42));
        close.setPadding(0, 0, 0, 0);
        close.setOnClickListener(v -> stopSelf());
        header.addView(close, new LinearLayout.LayoutParams(dp(48), dp(42)));
        container.addView(header);

        panelWebView = new WebView(this);
        panelWebView.setBackgroundColor(Color.rgb(7, 13, 25));
        panelWebView.getSettings().setJavaScriptEnabled(true);
        panelWebView.getSettings().setDomStorageEnabled(true);
        panelWebView.getSettings().setMediaPlaybackRequiresUserGesture(false);
        panelWebView.getSettings().setAllowFileAccess(false);
        panelWebView.getSettings().setAllowContentAccess(true);
        CookieManager.getInstance().setAcceptCookie(true);
        panelWebView.setWebChromeClient(new WebChromeClient());
        panelWebView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, android.webkit.WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (APP_HOST.equalsIgnoreCase(uri.getHost())) return false;
                return true;
            }
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                view.evaluateJavascript(
                    "(function(){var b=document.querySelector('[data-bv-go=\\\"baixar\\\"]');if(b)b.click();setTimeout(function(){var f=document.getElementById('form');if(f)f.scrollIntoView({block:'start'});},450);})();",
                    null
                );
            }
        });
        container.addView(panelWebView, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, 0, 1
        ));

        panel = container;
        int width = Math.min(getResources().getDisplayMetrics().widthPixels - dp(24), dp(520));
        int height = (int) (getResources().getDisplayMetrics().heightPixels * 0.78f);
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
            width, height,
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        );
        params.gravity = Gravity.CENTER;
        params.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE;
        try {
            windowManager.addView(panel, params);
            panelWebView.loadUrl(HOME_URL);
        } catch (Exception e) {
            panelOpen = false;
            panel = null;
        }
    }

    private void closePanel() {
        if (panelWebView != null) {
            panelWebView.stopLoading();
            panelWebView.destroy();
            panelWebView = null;
        }
        if (panel != null) {
            try { windowManager.removeView(panel); } catch (Exception ignored) { }
            panel = null;
        }
        panelOpen = false;
    }

    @Override
    public void onDestroy() {
        closePanel();
        if (bubble != null) {
            try { windowManager.removeView(bubble); } catch (Exception ignored) { }
            bubble = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
