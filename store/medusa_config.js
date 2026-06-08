/**
 * store/medusa_config.js — MedusaJS V2 Configuration
 * 
 * Full ecommerce store config for a dropshipping operation.
 * Supports: multi-currency, multi-region, Stripe + MercadoPago,
 * WhatsApp cart recovery webhooks, Vercel deployment.
 * 
 * Install: npx create-medusa-app@latest --no-browser
 * Docs: https://docs.medusajs.com
 */

const { loadEnv } = require("@medusajs/framework/utils")
loadEnv(process.env.NODE_ENV || "development", process.cwd())

/** @type {import('@medusajs/medusa/dist/types/global').ConfigModule} */
module.exports = {
  projectConfig: {
    databaseUrl: process.env.DATABASE_URL,
    databaseDriverOptions: { ssl: process.env.NODE_ENV === "production" ? { rejectUnauthorized: false } : false },
    redisUrl: process.env.REDIS_URL,
    workerMode: process.env.MEDUSA_WORKER_MODE || "shared",
    http: {
      adminCors: process.env.ADMIN_CORS || "http://localhost:7001",
      storeCors: process.env.STORE_CORS || "http://localhost:8001",
      authCors: process.env.AUTH_CORS || "http://localhost:7001",
      jwtSecret: process.env.JWT_SECRET || "change-me-in-production",
      cookieSecret: process.env.COOKIE_SECRET || "change-me-in-production",
    },
  },

  admin: {
    disable: false,
    backendUrl: process.env.MEDUSA_BACKEND_URL || "http://localhost:9000",
  },

  modules: [
    // ── Payment: Stripe ──────────────────────────────────────────────────────
    {
      resolve: "@medusajs/payment-stripe",
      options: {
        apiKey: process.env.STRIPE_SECRET_KEY,
        webhookSecret: process.env.STRIPE_WEBHOOK_SECRET,
        capture: true, // Auto-capture payments
      },
    },

    // ── File Storage: S3 (DigitalOcean Spaces or AWS) ────────────────────────
    {
      resolve: "@medusajs/file-s3",
      options: {
        s3Region: process.env.S3_REGION || "nyc3",
        s3Bucket: process.env.S3_BUCKET || "ecommerce-ai-assets",
        s3Endpoint: process.env.S3_ENDPOINT,  // For DO Spaces
        s3AccessKeyId: process.env.S3_ACCESS_KEY,
        s3SecretAccessKey: process.env.S3_SECRET_KEY,
        s3FileUrl: process.env.S3_PUBLIC_URL,
      },
    },

    // ── Notifications: SendGrid ──────────────────────────────────────────────
    {
      resolve: "@medusajs/notification-sendgrid",
      options: {
        apiKey: process.env.SENDGRID_API_KEY,
        from: process.env.SENDGRID_FROM_EMAIL || "hello@yourbrand.com",
        emailTemplates: {
          order_placed:      { id: process.env.SENDGRID_ORDER_TEMPLATE },
          order_shipped:     { id: process.env.SENDGRID_SHIPPED_TEMPLATE },
          password_reset:    { id: process.env.SENDGRID_RESET_TEMPLATE },
        },
      },
    },

    // ── Cache: Redis ─────────────────────────────────────────────────────────
    {
      resolve: "@medusajs/cache-redis",
      options: {
        redisUrl: process.env.REDIS_URL,
        ttl: 30, // seconds
      },
    },

    // ── Events: Redis (for cart abandonment webhooks) ────────────────────────
    {
      resolve: "@medusajs/event-bus-redis",
      options: { redisUrl: process.env.REDIS_URL },
    },
  ],

  plugins: [],
}
