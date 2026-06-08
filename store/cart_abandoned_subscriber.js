/**
 * store/cart_abandoned_subscriber.js
 * 
 * MedusaJS subscriber: fires when cart is abandoned for >30 min.
 * Calls FastAPI /api/webhooks/cart-abandoned → WhatsApp recovery sequence.
 * 
 * Place in: src/subscribers/cart-abandoned.ts
 */

import { SubscriberArgs, type SubscriberConfig } from "@medusajs/framework"

// Cart abandoned event fires after 30 min inactivity (configured in medusa-config)
export default async function cartAbandonedHandler({ event: { data }, container }: SubscriberArgs<any>) {
  const { id: cartId } = data
  
  try {
    const cartService = container.resolve("cartModuleService")
    const cart = await cartService.retrieveCart(cartId, { relations: ["items", "customer"] })
    
    if (!cart.customer?.phone) return  // Skip if no phone number
    
    const payload = {
      customer_phone: cart.customer.phone,
      customer_name:  cart.customer.first_name || "there",
      product_name:   cart.items?.[0]?.title || "your item",
      product_image_url: cart.items?.[0]?.thumbnail,
      cart_value_usd: parseFloat(cart.total) / 100,
      cart_id:        cartId,
      niche:          process.env.STORE_NICHE || "lifestyle",
      store_url:      process.env.STORE_URL || "https://yourstore.com",
      tenant_id:      process.env.TENANT_ID || "default",
    }
    
    await fetch(`${process.env.AI_API_URL}/api/webhooks/cart-abandoned`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    })
    
    console.log(`[cart-abandoned] Recovery triggered for cart ${cartId}`)
  } catch (error) {
    console.error("[cart-abandoned] Error:", error)
  }
}

export const config: SubscriberConfig = {
  event: "cart.created", // Replace with actual cart abandonment event name
}
