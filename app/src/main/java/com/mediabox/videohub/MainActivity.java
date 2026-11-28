package com.mediabox.videohub;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;

public class MainActivity extends Activity {
    private static final String HOME_URL = "https://mediabox-lab-staging.up.railway.app/";
    private static final String APP_HOST = "mediabox-lab-staging.up.railway.app";
    private static final int REQUEST_WRITE_STORAGE = 4102;

    private WebView webView;
    private FrameLayout root;
    private ProgressBar progress;
    private TextView errorView;
    private PendingDownload pendingDownload;

    private static final class PendingDownload {
        final String url;
        final String userAgent;
        final String contentDisposition;
        final String mimeType;

        PendingDownload(String url, String userAgent, String contentDisposition, String mimeType) {
            this.url = url;
            this.userAgent = userAgent;
            this.contentDisposition = contentDisposition;
            this.mimeType = mimeType;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.rgb(7, 10, 18));
        getWindow().setNavigationBarColor(Color.rgb(7, 10, 18));

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(7, 10, 18));

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost() == null ? "" : uri.getHost();
                if (host.equalsIgnoreCase(APP_HOST)) return false;
                if ("https".equalsIgnoreCase(uri.getScheme()) || "http".equalsIgnoreCase(uri.getScheme())) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    } catch (Exception ignored) { }
                    return true;
                }
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showError();
            }
        });

        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            if (url == null || !url.toLowerCase(Locale.ROOT).startsWith("https://")) {
                Toast.makeText(this, "Link de download inseguro ou inválido.", Toast.LENGTH_LONG).show();
                return;
            }
            pendingDownload = new PendingDownload(url, userAgent, contentDisposition, mimeType);
            if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P
                    && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQUEST_WRITE_STORAGE);
                return;
            }
            iniciarDownload(pendingDownload);
        });

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        FrameLayout.LayoutParams progressParams = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, 5, Gravity.TOP);
        root.addView(webView, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        root.addView(progress, progressParams);

        errorView = new TextView(this);
        errorView.setText("Não foi possível conectar ao MEDIABOX.\nVerifique sua internet e toque para tentar novamente.");
        errorView.setTextColor(Color.WHITE);
        errorView.setTextSize(16);
        errorView.setGravity(Gravity.CENTER);
        errorView.setPadding(32, 32, 32, 32);
        errorView.setVisibility(View.GONE);
        errorView.setOnClickListener(v -> {
            errorView.setVisibility(View.GONE);
            webView.setVisibility(View.VISIBLE);
            webView.loadUrl(HOME_URL);
        });
        root.addView(errorView, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        setContentView(root);
        if (savedInstanceState != null) webView.restoreState(savedInstanceState);
        else webView.loadUrl(HOME_URL);
    }

    private void iniciarDownload(PendingDownload item) {
        if (item == null) return;
        try {
            String fileName = URLUtil.guessFileName(item.url, item.contentDisposition, item.mimeType);
            if (fileName == null || fileName.trim().isEmpty()) fileName = "mediabox_video.mp4";
            fileName = fileName.replaceAll("[\\\\/:*?\\"<>|]", "_").trim();
            if (fileName.isEmpty()) fileName = "mediabox_video.mp4";

            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(item.url));
            request.setTitle(fileName);
            request.setDescription("Download realizado pelo MEDIABOX");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setAllowedOverMetered(true);
            request.setAllowedOverRoaming(false);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
            if (item.mimeType != null && !item.mimeType.trim().isEmpty()) {
                request.setMimeType(item.mimeType);
            }

            String userAgent = item.userAgent;
            if (userAgent == null || userAgent.trim().isEmpty()) {
                userAgent = webView.getSettings().getUserAgentString();
            }
            if (userAgent != null && !userAgent.trim().isEmpty()) {
                request.addRequestHeader("User-Agent", userAgent);
            }

            String cookie = CookieManager.getInstance().getCookie(item.url);
            if (cookie != null && !cookie.isEmpty()) {
                request.addRequestHeader("Cookie", cookie);
            }
            request.addRequestHeader("Referer", HOME_URL);

            DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) throw new IllegalStateException("Gerenciador de downloads indisponível.");
            manager.enqueue(request);
            Toast.makeText(this, "Download iniciado. Acompanhe pela notificação e pela pasta Downloads.", Toast.LENGTH_LONG).show();
            pendingDownload = null;
        } catch (Exception e) {
            Toast.makeText(this, "Não foi possível iniciar o download: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_WRITE_STORAGE) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                iniciarDownload(pendingDownload);
            } else {
                Toast.makeText(this, "Permissão necessária para salvar na pasta Downloads.", Toast.LENGTH_LONG).show();
                pendingDownload = null;
            }
        }
    }

    private void showError() {
        if (errorView != null) {
            webView.setVisibility(View.GONE);
            errorView.setVisibility(View.VISIBLE);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
    }
}
